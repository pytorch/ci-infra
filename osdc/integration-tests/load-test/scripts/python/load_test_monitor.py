"""Load test monitoring: polls GitHub Actions runs and reports results."""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

from distribution import RunnerAllocation
from run import CANARY_REPO, POLL_INTERVAL_SECONDS, format_duration, run_cmd
from workflow_generator import sanitize_job_key

log = logging.getLogger("osdc-load-test")

DEFAULT_TIMEOUT_MINUTES = 120


@dataclass
class JobResult:
    """Result of a single load test job."""

    name: str
    conclusion: str
    runner_type: str


@dataclass
class LoadTestResults:
    """Aggregated load test results."""

    total_expected: int
    completed_jobs: int
    timed_out: bool
    duration_seconds: float
    jobs: list[JobResult] = field(default_factory=list)
    run_ids: list[int] = field(default_factory=list)


def parse_runner_type(job_name: str) -> str | None:
    """Extract runner type from a load test job name.

    Job names follow the pattern: 'load-{sanitized_label} ({index})'
    Returns the sanitized label, or None if the name doesn't match.
    """
    m = re.match(r"^load-(.+?)(?:\s*\(\d+\))?$", job_name)
    return m.group(1) if m else None


def _build_label_lookup(
    allocations: list[RunnerAllocation],
) -> dict[str, str]:
    """Build a mapping from sanitized job key to original OSDC label.

    Also maps part-suffixed keys (e.g., 'l-x86iavx512-8-16-part0') back to
    the original OSDC label, so that split jobs are correctly attributed.
    """
    lookup: dict[str, str] = {}
    for a in allocations:
        key = sanitize_job_key(a.osdc_label)
        lookup[key] = a.osdc_label
        # Pre-populate part-suffixed keys for split jobs (up to 10 parts)
        for i in range(10):
            lookup[f"{key}-part{i}"] = a.osdc_label
    return lookup


def wait_for_load_test(
    branch: str,
    pr_created_at: datetime,
    allocations: list[RunnerAllocation],
    timeout_minutes: int = DEFAULT_TIMEOUT_MINUTES,
) -> LoadTestResults:
    """Poll for load test workflow completion with per-type progress tracking."""
    total_expected = sum(a.job_count for a in allocations)
    label_lookup = _build_label_lookup(allocations)
    deadline = time.time() + timeout_minutes * 60
    start_time = time.time()

    log.info(
        "Waiting for %d jobs (timeout: %d min)...",
        total_expected,
        timeout_minutes,
    )

    while time.time() < deadline:
        runs = _get_filtered_runs(branch, pr_created_at)
        if not runs:
            log.info("  No runs found yet, waiting...")
            time.sleep(POLL_INTERVAL_SECONDS)
            continue

        # Check if all runs are completed
        all_done = all(r.get("status") == "completed" for r in runs)
        if not all_done:
            _print_progress(runs, label_lookup, total_expected)
            time.sleep(POLL_INTERVAL_SECONDS)
            continue

        # All runs completed -- collect job details
        jobs, run_ids = _collect_job_results(runs, label_lookup)
        duration = time.time() - start_time
        log.info(
            "All runs completed in %s. Collected %d jobs.",
            format_duration(duration),
            len(jobs),
        )
        return LoadTestResults(
            total_expected=total_expected,
            completed_jobs=len(jobs),
            timed_out=False,
            duration_seconds=duration,
            jobs=jobs,
            run_ids=run_ids,
        )

    # Timeout
    duration = time.time() - start_time
    log.warning("Timeout reached after %s!", format_duration(duration))
    runs = _get_filtered_runs(branch, pr_created_at)
    jobs, run_ids = _collect_job_results(runs, label_lookup) if runs else ([], [])

    return LoadTestResults(
        total_expected=total_expected,
        completed_jobs=len(jobs),
        timed_out=True,
        duration_seconds=duration,
        jobs=jobs,
        run_ids=run_ids,
    )


def _get_filtered_runs(
    branch: str,
    not_before: datetime,
) -> list[dict]:
    """Get workflow runs on the branch created at or after not_before."""
    result = run_cmd(
        [
            "gh", "run", "list",
            "--repo", CANARY_REPO,
            "--branch", branch,
            "--json", "databaseId,status,conclusion,name,createdAt",
        ],
        check=False,
    )
    if result.returncode != 0:
        log.warning("Could not list runs: %s", result.stderr.strip())
        return []

    all_runs = json.loads(result.stdout) if result.stdout.strip() else []
    filtered = []
    for r in all_runs:
        created = r.get("createdAt", "")
        if not created:
            filtered.append(r)
            continue
        try:
            created_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
            if created_dt >= not_before:
                filtered.append(r)
        except (ValueError, TypeError):
            filtered.append(r)
    return filtered


def _collect_job_results(
    runs: list[dict],
    label_lookup: dict[str, str],
) -> tuple[list[JobResult], list[int]]:
    """Fetch job details from completed runs."""
    jobs: list[JobResult] = []
    run_ids: list[int] = []

    for run in runs:
        run_id = run["databaseId"]
        run_ids.append(run_id)

        result = run_cmd(
            [
                "gh", "run", "view", str(run_id),
                "--repo", CANARY_REPO,
                "--json", "jobs",
            ],
            check=False,
        )
        if result.returncode != 0 or not result.stdout.strip():
            continue

        run_data = json.loads(result.stdout)
        for job in run_data.get("jobs", []):
            name = job.get("name", "")
            runner_key = parse_runner_type(name)
            if runner_key is None:
                # Skip non-load-test system jobs
                continue
            conclusion = job.get("conclusion", "unknown")
            runner_type = label_lookup.get(runner_key, runner_key)
            jobs.append(JobResult(
                name=name,
                conclusion=conclusion,
                runner_type=runner_type,
            ))

    return jobs, run_ids


def _print_progress(
    runs: list[dict],
    label_lookup: dict[str, str],
    total_expected: int,
) -> None:
    """Print per-run progress summary."""
    completed = sum(1 for r in runs if r.get("status") == "completed")
    in_progress = len(runs) - completed
    log.info(
        "  %d/%d runs completed, %d in progress...",
        completed,
        len(runs),
        in_progress,
    )


def print_load_test_report(
    cluster_id: str,
    cluster_name: str,
    results: LoadTestResults,
) -> bool:
    """Print the load test summary report. Returns True if all jobs passed."""
    now = datetime.now(tz=UTC).strftime("%Y-%m-%d %H:%M UTC")
    overall_pass = True

    print("\n")
    print("=" * 70)
    print("  OSDC Load Test Results")
    print("=" * 70)
    print(f"  Cluster:  {cluster_id} ({cluster_name})")
    print(f"  Date:     {now}")
    print(f"  Duration: {format_duration(results.duration_seconds)}")
    print(f"  Jobs:     {results.completed_jobs}/{results.total_expected}")
    if results.timed_out:
        print("  WARNING:  Test timed out!")
        overall_pass = False
    print()

    # Group jobs by runner type
    by_type: dict[str, list[JobResult]] = {}
    for job in results.jobs:
        by_type.setdefault(job.runner_type, []).append(job)

    # Summary table
    print(f"  {'Runner Type':<40s} {'Pass':>6s} {'Fail':>6s} {'Total':>6s}")
    print(f"  {'-' * 40} {'-' * 6} {'-' * 6} {'-' * 6}")

    for runner_type in sorted(by_type):
        jobs = by_type[runner_type]
        passed = sum(1 for j in jobs if j.conclusion == "success")
        failed = len(jobs) - passed
        if failed > 0:
            overall_pass = False
        icon = "\u2713" if failed == 0 else "\u2717"
        print(
            f"  {icon} {runner_type:<38s} {passed:>6d} {failed:>6d} {len(jobs):>6d}",
        )

    print()

    # Failed job details
    failed_jobs = [j for j in results.jobs if j.conclusion != "success"]
    if failed_jobs:
        print(f"  Failed jobs ({len(failed_jobs)}):")
        for job in failed_jobs[:20]:
            print(f"    \u2717 {job.name} ({job.conclusion})")
        if len(failed_jobs) > 20:
            print(f"    ... and {len(failed_jobs) - 20} more")
        print()

    # Overall verdict
    if overall_pass:
        print("  Overall: PASSED")
    else:
        print("  Overall: FAILED")
    print("=" * 70)
    print()

    return overall_pass
