"""Validation and reporting phase functions for the OSDC integration test orchestrator (phases 3-5).

Phase 3: Parallel validation (smoke + compactor tests)
Phase 4: Collect workflow results + observability verification
Phase 5: Cleanup + report
"""

import json
import logging
import os
import subprocess
import sys
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


def wait_for_workflows(branch: str) -> list[dict]:
    """Poll for workflow run completion. Returns list of run results."""
    log.info("Phase 4: Waiting for PR workflow runs (timeout: %d min)...", WORKFLOW_TIMEOUT_MINUTES)

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

        runs = json.loads(result.stdout) if result.stdout.strip() else []
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
             "--json", "databaseId,status,conclusion,name"],
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            completed_runs = json.loads(result.stdout)

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


# ── Phase 4b: Observability verification ────────────────────────────────


def verify_observability(cluster_name: str, cfg: dict, upstream_dir: Path) -> list[dict]:
    """Verify metrics and logs are arriving in Grafana Cloud."""
    log.info("Phase 4b: Verifying observability...")
    results = []

    # Import smoke test helpers
    helpers_path = upstream_dir / "tests" / "smoke"
    sys.path.insert(0, str(helpers_path))
    try:
        from helpers import fetch_grafana_cloud_credentials, loki_read_url, mimir_read_url, query_loki, query_mimir
    except ImportError:
        log.warning("  Could not import smoke test helpers. Skipping observability checks.")
        results.append({"name": "observability", "status": "skip", "detail": "helpers not importable"})
        return results

    # ── Mimir (metrics) ──
    mimir_url = resolve(cfg, "monitoring.grafana_cloud_url")
    if not mimir_url:
        results.append({"name": "Mimir", "status": "skip", "detail": "monitoring.grafana_cloud_url not configured"})
    else:
        creds = fetch_grafana_cloud_credentials("monitoring", "username", "password")
        if not creds:
            results.append({"name": "Mimir", "status": "skip", "detail": "monitoring credentials not configured"})
        else:
            read_url = mimir_read_url(mimir_url)
            username, password = creds

            metrics_checks = [
                ("runner pod metrics (kube_pod_status_phase)",
                 f'kube_pod_status_phase{{cluster="{cluster_name}", namespace="arc-runners", phase="Running"}}'),
                ("node-exporter metrics (node_cpu_seconds_total)",
                 f'node_cpu_seconds_total{{cluster="{cluster_name}"}}'),
            ]

            for name, promql in metrics_checks:
                try:
                    data = query_mimir(read_url, promql, username, password)
                    if data and data.get("status") == "success":
                        result_data = data.get("data", {}).get("result", [])
                        if result_data:
                            results.append({"name": f"Mimir: {name}", "status": "pass", "detail": ""})
                        else:
                            results.append({"name": f"Mimir: {name}", "status": "fail",
                                            "detail": "query succeeded but no data"})
                    else:
                        results.append({"name": f"Mimir: {name}", "status": "fail",
                                        "detail": f"query status: {data.get('status') if data else 'no response'}"})
                except Exception as e:
                    results.append({"name": f"Mimir: {name}", "status": "skip", "detail": str(e)})

    # ── Loki (logs) ──
    loki_url = resolve(cfg, "logging.grafana_cloud_loki_url")
    if not loki_url:
        results.append({"name": "Loki", "status": "skip", "detail": "logging.grafana_cloud_loki_url not configured"})
    else:
        creds = fetch_grafana_cloud_credentials("logging", "loki-username", "loki-api-key-read")
        if not creds:
            results.append({"name": "Loki", "status": "skip", "detail": "logging credentials not configured"})
        else:
            read_url = loki_read_url(loki_url)
            username, password = creds

            log_checks = [
                ("runner logs (namespace=arc-runners)",
                 f'{{cluster="{cluster_name}", namespace="arc-runners"}}'),
                ("ARC controller logs (namespace=arc-systems)",
                 f'{{cluster="{cluster_name}", namespace="arc-systems"}}'),
            ]

            for name, logql in log_checks:
                try:
                    data = query_loki(read_url, logql, username, password)
                    if data and data.get("status") == "success":
                        streams = data.get("data", {}).get("result", [])
                        if streams:
                            results.append({"name": f"Loki: {name}", "status": "pass", "detail": ""})
                        else:
                            results.append({"name": f"Loki: {name}", "status": "fail",
                                            "detail": "query succeeded but no streams"})
                    else:
                        results.append({"name": f"Loki: {name}", "status": "fail",
                                        "detail": f"query status: {data.get('status') if data else 'no response'}"})
                except Exception as e:
                    results.append({"name": f"Loki: {name}", "status": "skip", "detail": str(e)})

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
    observability_results: list[dict],
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

    # Validation results
    for name in ["smoke", "compactor"]:
        res = validation_results.get(name, {})
        status = res.get("status", "unknown")
        icon = "\u2713" if status == "passed" else ("\u2298" if status == "skipped" else "\u2717")
        label = name.capitalize()
        if status != "passed" and status != "skipped":
            overall_pass = False
        print(f"  {label:16s} {icon} {status.upper()}")
    print()

    # Observability
    if observability_results:
        print("  Observability:")
        for res in observability_results:
            status = res["status"]
            name = res["name"]
            detail = res.get("detail", "")
            if status == "pass":
                icon = "\u2713"
            elif status == "skip":
                icon = "\u2298"
            else:
                icon = "\u2717"
                overall_pass = False
            suffix = f" ({detail})" if detail else ""
            print(f"    {icon} {name}{suffix}")
        print()

    # Overall
    if overall_pass:
        print("  Overall: PASSED")
    else:
        print("  Overall: FAILED")
    print("=" * 60)
    print()

    return overall_pass
