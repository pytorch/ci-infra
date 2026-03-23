"""Validation and reporting phase functions for the OSDC integration test orchestrator (phases 3-5).

Phase 3: Parallel validation (smoke + compactor tests)
Phase 4: Collect workflow results
Phase 5: Cleanup + report
"""

import json
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
    resolve,
    run_cmd,
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
    env.update({
        "OSDC_ROOT": str(root_dir),
        "OSDC_UPSTREAM": str(upstream_dir),
        "CLUSTERS_YAML": str(root_dir / "clusters.yaml"),
    })

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

    # Wait for all processes
    for name, proc in procs.items():
        stdout, _ = proc.communicate()
        results[name] = {
            "status": "passed" if proc.returncode == 0 else "failed",
            "output": stdout,
            "returncode": proc.returncode,
        }
        log.info("  %s: %s (exit %d)", name, results[name]["status"], proc.returncode)

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


def wait_for_workflows(branch: str, pr_created_at: datetime) -> list[dict]:
    """Poll for workflow run completion. Returns list of run results.

    Only considers runs created at or after pr_created_at, so historical
    runs from previous integration test cycles on the same branch are excluded.
    """
    log.info("Phase 4: Waiting for PR workflow runs (timeout: %d min)...", WORKFLOW_TIMEOUT_MINUTES)
    log.info("  Filtering to runs created after %s", pr_created_at.isoformat())

    deadline = time.time() + WORKFLOW_TIMEOUT_MINUTES * 60
    completed_runs = []

    while time.time() < deadline:
        result = run_cmd(
            ["gh", "run", "list", "--repo", CANARY_REPO, "--branch", branch,
             "--json", "databaseId,status,conclusion,name,createdAt"],
            check=False,
        )
        if result.returncode != 0:
            log.warning("  Could not list runs: %s", result.stderr.strip())
            time.sleep(POLL_INTERVAL_SECONDS)
            continue

        all_runs = json.loads(result.stdout) if result.stdout.strip() else []
        runs = _filter_runs_by_time(all_runs, pr_created_at)
        if not runs:
            log.info("  No runs found yet, waiting...")
            time.sleep(POLL_INTERVAL_SECONDS)
            continue

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
        result = run_cmd(
            ["gh", "run", "list", "--repo", CANARY_REPO, "--branch", branch,
             "--json", "databaseId,status,conclusion,name,createdAt"],
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            completed_runs = _filter_runs_by_time(json.loads(result.stdout), pr_created_at)

    # Get job details for each run
    results = []
    for run in completed_runs:
        run_id = run["databaseId"]
        result = run_cmd(
            ["gh", "run", "view", str(run_id), "--repo", CANARY_REPO, "--json", "jobs"],
            check=False,
        )
        jobs = []
        if result.returncode == 0 and result.stdout.strip():
            run_data = json.loads(result.stdout)
            jobs = run_data.get("jobs", [])

        # Get failure logs if needed
        failure_log = ""
        if run.get("conclusion") == "failure":
            log_result = run_cmd(
                ["gh", "run", "view", str(run_id), "--repo", CANARY_REPO, "--log-failed"],
                check=False,
            )
            if log_result.returncode == 0:
                failure_log = log_result.stdout[:5000]  # Truncate to 5k chars

        results.append({
            "run_id": run_id,
            "name": run.get("name", "unknown"),
            "status": run.get("status", "unknown"),
            "conclusion": run.get("conclusion", "unknown"),
            "jobs": jobs,
            "failure_log": failure_log,
        })

    return results


# ── Phase 5: Cleanup + Report ───────────────────────────────────────────


def close_pr(pr_number: int):
    """Close the integration test PR."""
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
):
    """Print the final summary report."""
    now = datetime.now(tz=UTC).strftime("%Y-%m-%d %H:%M UTC")
    overall_pass = True

    print("\n")
    print("=" * 60)
    print("  OSDC Integration Test Results")
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
                conclusion = job.get("conclusion", "unknown")
                icon = "\u2713" if conclusion == "success" else "\u2717"
                if conclusion != "success":
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
        icon = "\u2713" if status == "passed" else ("\u2298" if status == "skipped" else "\u2717")
        label = name.capitalize()
        if status != "passed" and status != "skipped":
            overall_pass = False
        print(f"  {label:16s} {icon} {status.upper()}")
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
