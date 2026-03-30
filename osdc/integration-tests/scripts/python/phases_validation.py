"""Validation and reporting phase functions for the OSDC integration test orchestrator (phases 3-5).

Phase 3: Parallel validation (smoke + compactor tests)
Phase 4: Collect workflow results
Phase 5: Cleanup + report
"""

import logging
import os
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

from run import (
    CANARY_REPO,
    POLL_INTERVAL_SECONDS,
    WORKFLOW_TIMEOUT_MINUTES,
    format_duration,
    resolve,
    run_cmd,
    safe_json_loads,
)

log = logging.getLogger("osdc-integration-test")


# ── Phase 3: Parallel validation ───────────────────────────────────────


def run_parallel_validation(
    cluster_id: str,
    root_dir: Path,
    upstream_dir: Path,
    skip_smoke: bool,
    skip_compactor: bool,
    cfg: dict,
) -> dict:
    """Run smoke tests and compactor tests in parallel."""
    log.info("Phase 3: Running parallel validation...")
    results = {}
    procs = {}

    env = os.environ.copy()
    env.update(
        {
            "OSDC_ROOT": str(root_dir),
            "OSDC_UPSTREAM": str(upstream_dir),
            "CLUSTERS_YAML": str(root_dir / "clusters.yaml"),
        }
    )

    if not skip_smoke:
        log.info("  Starting smoke tests...")
        procs["smoke"] = subprocess.Popen(
            ["just", "smoke", cluster_id],
            cwd=root_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
        )
    else:
        results["smoke"] = {"status": "skipped"}

    compactor_enabled = resolve(cfg, "node_compactor.enabled", True)
    if not skip_compactor and compactor_enabled:
        log.info("  Starting compactor e2e tests...")
        procs["compactor"] = subprocess.Popen(
            ["just", "test-compactor", cluster_id],
            cwd=root_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
        )
    else:
        results["compactor"] = {"status": "skipped"}

    # Track start time for each process
    start_times = {name: time.monotonic() for name in procs}

    # Wait for all processes
    try:
        for name, proc in procs.items():
            stdout, _ = proc.communicate()
            elapsed = time.monotonic() - start_times[name]
            results[name] = {
                "status": "passed" if proc.returncode == 0 else "failed",
                "output": stdout,
                "returncode": proc.returncode,
                "duration_s": elapsed,
            }
            log.info("  %s: %s (exit %d)", name, results[name]["status"], proc.returncode)
    except KeyboardInterrupt:
        log.warning("  Interrupted during parallel validation")
        for name, proc in procs.items():
            if name in results:
                continue  # already collected
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            elapsed = time.monotonic() - start_times.get(name, time.monotonic())
            results[name] = {"status": "interrupted", "duration_s": elapsed}
            log.info("  %s: interrupted", name)
        raise

    return results


# ── Phase 4: Collect workflow results ───────────────────────────────────


def _filter_runs_by_time(runs: list[dict], not_before: datetime) -> list[dict]:
    """Filter runs to only those created at or after not_before."""
    filtered = []
    for r in runs:
        created = r.get("createdAt", "")
        if not created:
            filtered.append(r)  # keep if no timestamp (shouldn't happen)
            continue
        # gh returns ISO 8601 with Z suffix, e.g. "2026-03-20T23:14:05Z"
        try:
            created_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
            if created_dt >= not_before:
                filtered.append(r)
        except (ValueError, TypeError):
            filtered.append(r)  # keep if parse fails
    return filtered


def wait_for_workflows(
    branch: str,
    pr_created_at: datetime,
    workflow_name: str | None = None,
) -> list[dict]:
    """Poll for workflow run completion. Returns list of run results.

    Only considers runs created at or after pr_created_at, so historical
    runs from previous integration test cycles on the same branch are excluded.

    Args:
        workflow_name: If set, only track runs matching this workflow name.
    """
    # Leave a safety buffer so the GHA job has time for cleanup (PR close,
    # report printing) even when the polling loop runs long.
    CLEANUP_BUFFER_MINUTES = 10
    effective_timeout = max(WORKFLOW_TIMEOUT_MINUTES - CLEANUP_BUFFER_MINUTES, 10)

    log.info(
        "Phase 4: Waiting for PR workflow runs (timeout: %d min, buffer: %d min)...",
        effective_timeout,
        CLEANUP_BUFFER_MINUTES,
    )
    log.info("  Filtering to runs created after %s", pr_created_at.isoformat())
    if workflow_name:
        log.info("  Filtering to workflow name: %s", workflow_name)

    deadline = time.time() + effective_timeout * 60
    completed_runs = []
    logged_run_ids: set[int] = set()

    try:
        while time.time() < deadline:
            result = run_cmd(
                [
                    "gh",
                    "run",
                    "list",
                    "--repo",
                    CANARY_REPO,
                    "--branch",
                    branch,
                    "--json",
                    "databaseId,status,conclusion,name,createdAt",
                ],
                check=False,
            )
            if result.returncode != 0:
                log.warning("  Could not list runs: %s", result.stderr.strip())
                time.sleep(POLL_INTERVAL_SECONDS)
                continue

            all_runs = safe_json_loads(result.stdout, "list workflow runs") or []
            runs = _filter_runs_by_time(all_runs, pr_created_at)
            if workflow_name:
                runs = [r for r in runs if r.get("name") == workflow_name]
            if not runs:
                log.info("  No runs found yet, waiting...")
                time.sleep(POLL_INTERVAL_SECONDS)
                continue

            # Log URLs for newly discovered runs
            for r in runs:
                run_id = r.get("databaseId")
                if run_id and run_id not in logged_run_ids:
                    logged_run_ids.add(run_id)
                    run_url = f"https://github.com/{CANARY_REPO}/actions/runs/{run_id}"
                    log.info("  Run: %s — %s", r.get("name", "?"), run_url)

            all_done = all(r.get("status") == "completed" for r in runs)
            in_progress = sum(1 for r in runs if r.get("status") != "completed")

            if all_done:
                log.info("  All %d run(s) completed.", len(runs))
                completed_runs = runs
                break

            log.info("  %d/%d runs still in progress...", in_progress, len(runs))
            time.sleep(POLL_INTERVAL_SECONDS)
        else:
            log.warning("  Timeout reached! Collecting partial results.")
            completed_runs = _fetch_latest_runs(branch, pr_created_at)
    except KeyboardInterrupt:
        log.warning("  Interrupted during workflow polling")
        raise  # Let main() handle cleanup and partial result collection

    # Get job details for each run
    return _collect_run_details(completed_runs)


def _fetch_latest_runs(branch: str, pr_created_at: datetime) -> list[dict]:
    """Fetch the latest run list from GitHub (used on timeout and interrupt)."""
    result = run_cmd(
        [
            "gh",
            "run",
            "list",
            "--repo",
            CANARY_REPO,
            "--branch",
            branch,
            "--json",
            "databaseId,status,conclusion,name,createdAt",
        ],
        check=False,
    )
    if result.returncode == 0 and result.stdout.strip():
        runs = safe_json_loads(result.stdout, "fetch latest runs")
        if runs:
            return _filter_runs_by_time(runs, pr_created_at)
    return []


def _collect_run_details(runs: list[dict]) -> list[dict]:
    """Fetch job details and failure logs for a list of workflow runs."""
    results = []
    for run in runs:
        run_id = run["databaseId"]
        conclusion = run.get("conclusion") or "in_progress"
        result = run_cmd(
            ["gh", "run", "view", str(run_id), "--repo", CANARY_REPO, "--json", "jobs"],
            check=False,
        )
        jobs = []
        if result.returncode == 0 and result.stdout.strip():
            run_data = safe_json_loads(result.stdout, f"run {run_id} details")
            if isinstance(run_data, dict):
                jobs = run_data.get("jobs", [])

        # Get failure logs only for completed failures
        failure_log = ""
        if conclusion == "failure":
            log_result = run_cmd(
                ["gh", "run", "view", str(run_id), "--repo", CANARY_REPO, "--log-failed"],
                check=False,
            )
            if log_result.returncode == 0:
                failure_log = log_result.stdout[:5000]  # Truncate to 5k chars

        results.append(
            {
                "run_id": run_id,
                "name": run.get("name", "unknown"),
                "status": run.get("status", "unknown"),
                "conclusion": conclusion,
                "jobs": jobs,
                "failure_log": failure_log,
            }
        )

    return results


# ── Phase 5: Cleanup + Report ───────────────────────────────────────────


def close_pr(pr_number: int, branch: str | None = None):
    """Close the integration test PR and cancel associated workflows.

    Args:
        branch: If provided, cancel queued/in-progress workflow runs on this
                branch before closing the PR.  Optional for backward compat.
    """
    if branch:
        for status in ("queued", "in_progress"):
            result = run_cmd(
                [
                    "gh",
                    "run",
                    "list",
                    "--repo",
                    CANARY_REPO,
                    "--branch",
                    branch,
                    "--status",
                    status,
                    "--json",
                    "databaseId",
                ],
                check=False,
            )
            if result.returncode != 0 or not result.stdout.strip():
                continue
            runs = safe_json_loads(result.stdout, f"list {status} runs")
            if not runs:
                continue
            for r in runs:
                log.info("  Cancelling %s run %s", status, r["databaseId"])
                run_cmd(
                    ["gh", "run", "cancel", str(r["databaseId"]), "--repo", CANARY_REPO],
                    check=False,
                )

    log.info("Phase 5: Closing PR #%d...", pr_number)
    run_cmd(
        ["gh", "pr", "close", str(pr_number), "--repo", CANARY_REPO, "--delete-branch"],
        check=False,
    )


def print_report(
    cluster_id: str,
    cluster_name: str,
    workflow_results: list[dict],
    validation_results: dict,
    interrupted: bool = False,
):
    """Print the final summary report."""
    now = datetime.now(tz=UTC).strftime("%Y-%m-%d %H:%M UTC")
    overall_pass = True

    title = "OSDC Integration Test Results"
    if interrupted:
        title += " (interrupted)"

    print("\n")
    print("=" * 60)
    print(f"  {title}")
    print("=" * 60)
    print(f"  Cluster: {cluster_id} ({cluster_name})")
    print(f"  Date:    {now}")
    print()

    # PR Workflow Jobs
    if workflow_results:
        print("  PR Workflow Jobs:")
        for run in workflow_results:
            for job in run.get("jobs", []):
                name = job.get("name", "unknown")
                conclusion = job.get("conclusion") or "in_progress"
                if conclusion == "success":
                    icon = "\u2713"
                elif conclusion == "in_progress":
                    icon = "\u2026"
                else:
                    icon = "\u2717"
                    overall_pass = False
                print(f"    {icon} {name:30s} {conclusion}")
            if run.get("failure_log"):
                print(f"    --- Failure log (run {run['run_id']}) ---")
                for line in run["failure_log"].split("\n")[:20]:
                    print(f"    | {line}")
                print("    ---")
        print()
    else:
        print("  PR Workflow Jobs: N/A (dry run or no runs)")
        print()

    # Validation results (with output on failure)
    for name in ["smoke", "compactor"]:
        res = validation_results.get(name, {})
        status = res.get("status", "unknown")
        if status == "passed":
            icon = "\u2713"
        elif status in ("skipped", "interrupted"):
            icon = "\u2298"
        else:
            icon = "\u2717"
        label = name.capitalize()
        if status not in ("passed", "skipped", "interrupted"):
            overall_pass = False
        duration = res.get("duration_s")
        dur_str = f" ({format_duration(duration)})" if duration is not None else ""
        print(f"  {label:16s} {icon} {status.upper()}{dur_str}")
        if status == "failed":
            output = res.get("output", "")
            if output:
                print(f"    --- {label} output (last 50 lines) ---")
                lines = output.rstrip("\n").split("\n")
                for line in lines[-50:]:
                    print(f"    | {line}")
                print("    ---")
    print()

    # Overall
    if overall_pass:
        print("  Overall: PASSED")
    else:
        print("  Overall: FAILED")
    print("=" * 60)
    print()

    return overall_pass
