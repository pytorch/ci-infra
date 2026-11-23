#!/usr/bin/env python3
"""OSDC Workload Test Orchestrator.

Mirrors pytorch/pytorch content onto pytorch-canary, instruments workflow
references and runner labels, and runs selected workflows against an OSDC
cluster to validate that production workloads execute correctly.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

# Add the shared integration test module directory to sys.path so we can
# import run.py, phases.py, and phases_validation.py with bare imports.
_INTEG_SCRIPTS = Path(__file__).resolve().parent.parent.parent.parent / "scripts" / "python"
if str(_INTEG_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_INTEG_SCRIPTS))

from phases import cleanup_stale_prs, ensure_canary_repo
from phases_validation import close_pr
from run import (
    CANARY_REPO,
    format_duration,
    has_module,
    load_cluster_config,
    resolve,
    run_cmd,
)
from workload_phases import (
    KEEP_WORKFLOWS,
    PR_TITLE_PREFIX,
    create_workload_pr,
    ensure_pytorch_repo,
    instrument_workflows,
    mirror_content,
    monitor_workflows,
    print_workload_report,
)

log = logging.getLogger("osdc-workload-test")

DEFAULT_TIMEOUT_MINUTES = 60
DEFAULT_POLL_INTERVAL = 30


def branch_name(cluster_id: str) -> str:
    """Return a cluster-specific branch name for workload tests."""
    return f"osdc-workload-test-{cluster_id}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="OSDC Workload Test Orchestrator")
    parser.add_argument(
        "--cluster-id",
        required=True,
        help="Cluster ID from clusters.yaml",
    )
    parser.add_argument(
        "--clusters-yaml",
        required=True,
        type=Path,
        help="Path to clusters.yaml",
    )
    parser.add_argument(
        "--upstream-dir",
        required=True,
        type=Path,
        help="OSDC upstream directory",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Prepare everything locally but don't push/create PR",
    )
    parser.add_argument(
        "--keep-pr",
        action="store_true",
        help="Don't close PR after test",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT_MINUTES,
        help=f"Workflow timeout in minutes (default: {DEFAULT_TIMEOUT_MINUTES})",
    )
    args = parser.parse_args()

    if args.timeout <= 0:
        parser.error("--timeout must be a positive integer")

    return args


def main() -> None:
    args = parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    cfg = load_cluster_config(args.clusters_yaml, args.cluster_id)
    cluster_name = resolve(cfg, "cluster_name")

    # Validate the cluster has an arc-runners module — without it, there are
    # no runner labels to target and the instrumented workflows would be broken.
    if not has_module(cfg, "arc-runners"):
        log.error(
            "Cluster '%s' does not have an 'arc-runners' module. "
            "Workload tests require a cluster with ARC runners configured.",
            args.cluster_id,
        )
        sys.exit(1)

    prefix = resolve(cfg, "arc-runners.runner_name_prefix", "")
    branch = branch_name(args.cluster_id)

    log.info("Workload test for cluster: %s (%s)", args.cluster_id, cluster_name)
    log.info("  Runner prefix: '%s'", prefix)
    log.info("  Keep workflows: %s", KEEP_WORKFLOWS)

    # Phase 0: Cleanup stale PRs
    log.info("Phase 0: Cleaning up stale PRs...")
    cleanup_stale_prs(branch, pr_title_prefix=PR_TITLE_PREFIX)

    # Phase 1: Prepare repos
    log.info("Phase 1: Preparing repos...")
    canary_path = ensure_canary_repo(args.upstream_dir)
    pytorch_path = ensure_pytorch_repo(args.upstream_dir)

    # Check out fresh branch on canary
    run_cmd(["git", "fetch", "origin", "main"], cwd=canary_path)
    run_cmd(["git", "checkout", "-B", branch, "origin/main"], cwd=canary_path)

    # Phase 2: Mirror content
    log.info("Phase 2: Mirroring pytorch content...")
    mirror_content(pytorch_path, canary_path)

    # Phase 3: Instrument workflows
    log.info("Phase 3: Instrumenting workflows...")
    instrument_workflows(canary_path, prefix)

    # Phase 4: Create PR
    pr_created_at = datetime.now(tz=UTC)
    pr_number = create_workload_pr(canary_path, args.cluster_id, args.dry_run, branch)

    if args.dry_run:
        log.info("DRY RUN complete. No PR created.")
        sys.exit(0)

    if pr_number is None:
        log.error("Failed to create PR.")
        sys.exit(1)

    # Phase 5 + 6: Monitor and report
    overall_pass = False
    interrupted = False
    try:
        results = monitor_workflows(
            branch,
            pr_created_at,
            timeout_minutes=args.timeout,
            poll_interval=DEFAULT_POLL_INTERVAL,
        )
        overall_pass = print_workload_report(
            args.cluster_id,
            cluster_name,
            results,
        )
    except KeyboardInterrupt:
        log.warning("Interrupted — collecting partial results...")
        interrupted = True
        # Attempt to collect whatever we have
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
        partial = []
        if result.returncode == 0 and result.stdout.strip():
            from phases_validation import _collect_run_details, _filter_runs_by_time
            from run import safe_json_loads

            all_runs = safe_json_loads(result.stdout, "fetch partial") or []
            partial = _collect_run_details(_filter_runs_by_time(all_runs, pr_created_at))
        print_workload_report(args.cluster_id, cluster_name, partial, interrupted=True)
    finally:
        if pr_number is not None and not args.keep_pr:
            log.info("Closing PR #%d...", pr_number)
            close_pr(pr_number, branch=branch)
        elif pr_number is not None:
            log.info("Keeping PR #%d open (--keep-pr).", pr_number)

    sys.exit(0 if overall_pass and not interrupted else 1)


if __name__ == "__main__":
    main()
