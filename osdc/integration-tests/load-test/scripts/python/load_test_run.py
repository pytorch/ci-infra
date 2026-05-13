#!/usr/bin/env python3
"""OSDC Load Test Orchestrator.

Runs a load test against an OSDC cluster by distributing ~N jobs
proportionally across all deployed runner types, monitoring completion,
and reporting results.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
from datetime import UTC, datetime
from pathlib import Path

# Add the shared integration test module directory to sys.path so we can
# import run.py and phases.py with bare imports (same pattern pytest uses).
_INTEG_SCRIPTS = Path(__file__).resolve().parent.parent.parent.parent / "scripts" / "python"
if str(_INTEG_SCRIPTS) not in sys.path:  # pragma: no cover
    sys.path.insert(0, str(_INTEG_SCRIPTS))

from distribution import (
    RunnerAllocation,
    classify_runner,
    compute_distribution,
    get_available_runners,
)
from load_test_monitor import print_load_test_report, wait_for_load_test
from phases import cleanup_stale_prs, ensure_canary_repo
from run import (
    CANARY_REPO,
    format_duration,
    load_cluster_config,
    resolve,
    run_cmd,
)
from workflow_generator import generate_workflow

log = logging.getLogger("osdc-load-test")

DEFAULT_TOTAL_JOBS = 400
LOAD_TEST_PR_TITLE_PREFIX = "[NO REVIEW][NO MERGE] ARC load test"


def _parse_label_spec(spec: str) -> tuple[str, int]:
    """Parse a '--label LABEL:COUNT' spec into (label, count)."""
    label, sep, count_str = spec.rpartition(":")
    if not sep or not label:
        raise argparse.ArgumentTypeError(
            f"invalid --label '{spec}': expected format 'LABEL:COUNT'",
        )
    try:
        count = int(count_str)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"invalid --label '{spec}': count must be an integer",
        ) from None
    if count <= 0:
        raise argparse.ArgumentTypeError(
            f"invalid --label '{spec}': count must be positive",
        )
    return (label, count)


def branch_name(cluster_id: str) -> str:
    """Return a cluster-specific branch name for load tests."""
    return f"osdc-load-test-{cluster_id}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="OSDC Load Test Orchestrator")
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
        "--root-dir",
        required=True,
        type=Path,
        help="OSDC root directory (consumer or upstream)",
    )
    parser.add_argument(
        "--label",
        type=_parse_label_spec,
        action="append",
        default=[],
        metavar="LABEL:COUNT",
        help=(
            "Run COUNT jobs on the specified runner label. Repeat for multiple labels "
            "(e.g., --label l-x86iamx-8-16:400 --label l-x86aavx2-29-113-a10g:200). "
            "Mutually exclusive with --jobs."
        ),
    )
    parser.add_argument(
        "--jobs",
        type=int,
        default=None,
        help=(
            f"Total number of jobs for proportional distribution across all runner types "
            f"(default: {DEFAULT_TOTAL_JOBS}). Ignored when --label is used."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Generate workflow but don't push/PR",
    )
    parser.add_argument(
        "--keep-pr",
        action="store_true",
        help="Don't close PR after test",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=120,
        help="Timeout in minutes (default: 120)",
    )
    args = parser.parse_args()

    if args.label and args.jobs is not None:
        parser.error("--label and --jobs are mutually exclusive")

    if args.label:
        seen: set[str] = set()
        for label, _ in args.label:
            if label in seen:
                parser.error(f"--label '{label}' specified more than once")
            seen.add(label)

    if args.jobs is None:
        args.jobs = DEFAULT_TOTAL_JOBS
    if args.jobs <= 0:
        parser.error("--jobs must be a positive integer")
    if args.timeout <= 0:
        parser.error("--timeout must be a positive integer")

    return args


def _print_distribution(allocations: list, cluster_id: str) -> None:
    """Print the job distribution table."""
    print(f"\n  Load test distribution for {cluster_id}:")
    print(f"  {'Runner Type':<40s} {'Jobs':>6s} {'Source (30d)':>12s} {'%':>7s} {'Type':>6s}")
    print(f"  {'-' * 40} {'-' * 6} {'-' * 12} {'-' * 7} {'-' * 6}")

    total_jobs = 0
    for a in allocations:
        rtype = "GPU" if a.is_gpu else ("ARM" if a.is_arm64 else "CPU")
        if a.is_gpu and a.gpu_count > 1:
            rtype = f"GPU*{a.gpu_count}"
        print(
            f"  {a.osdc_label:<40s} {a.job_count:>6d} {a.source_job_count:>12,d} {a.proportion:>6.1%} {rtype:>6s}",
        )
        total_jobs += a.job_count

    print(f"  {'-' * 40} {'-' * 6}")
    print(f"  {'Total':<40s} {total_jobs:>6d}")
    print()


def _sigterm_handler(signum, frame):
    """Convert SIGTERM to SystemExit so finally blocks execute."""
    raise SystemExit(128 + signum)


def main() -> None:
    args = parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    signal.signal(signal.SIGTERM, _sigterm_handler)

    cfg = load_cluster_config(args.clusters_yaml, args.cluster_id)
    cluster_name = resolve(cfg, "cluster_name")
    prefix = resolve(cfg, "arc-runners.runner_name_prefix", "")
    region = resolve(cfg, "region", "")
    branch = branch_name(args.cluster_id)

    log.info("Load test for cluster: %s (%s)", args.cluster_id, cluster_name)
    log.info("  Runner prefix: '%s'", prefix)
    if args.label:
        total_label_jobs = sum(c for _, c in args.label)
        log.info("  Label mode: %d label(s), %d total jobs", len(args.label), total_label_jobs)
    else:
        log.info("  Target jobs: %d", args.jobs)

    # Phase 1: Compute distribution
    available, excluded_count = get_available_runners(
        args.upstream_dir,
        args.root_dir,
        region=region,
    )
    log.info(
        "  Available runner types: %d (excluded %d: region %s + specialized GPUs)",
        len(available),
        excluded_count,
        region,
    )

    if args.label:
        missing = [lbl for lbl, _ in args.label if lbl not in available]
        if missing:
            log.error(
                "Runner label(s) not found: %s. Available labels:\n  %s",
                ", ".join(missing),
                "\n  ".join(sorted(available)),
            )
            sys.exit(1)
        total = sum(c for _, c in args.label)
        allocations = []
        for label, count in args.label:
            is_gpu, is_arm64, gpu_count = classify_runner(label)
            allocations.append(
                RunnerAllocation(
                    osdc_label=label,
                    job_count=count,
                    source_job_count=0,
                    proportion=count / total,
                    is_gpu=is_gpu,
                    is_arm64=is_arm64,
                    gpu_count=gpu_count,
                ),
            )
    else:
        allocations = compute_distribution(args.jobs, available)
        if not allocations:
            log.error("No runner types available. Cannot run load test.")
            sys.exit(1)

    _print_distribution(allocations, args.cluster_id)

    # Phase 2: Generate workflow
    workflow_content = generate_workflow(allocations, prefix, args.cluster_id)

    if args.dry_run:
        print("--- Generated workflow YAML ---")
        print(workflow_content)
        print("--- End workflow YAML ---")
        log.info("DRY RUN complete. No PR created.")
        sys.exit(0)

    # Phase 3: Cleanup stale PRs
    cleanup_stale_prs(branch, pr_title_prefix=LOAD_TEST_PR_TITLE_PREFIX)

    # Phase 4: Clone canary + open PR
    canary_path = ensure_canary_repo(args.upstream_dir)
    pr_created_at = datetime.now(tz=UTC)
    pr_number = _prepare_load_test_pr(
        canary_path,
        args.upstream_dir,
        workflow_content,
        branch,
    )

    if pr_number is None:
        log.error("Failed to create PR.")
        sys.exit(1)

    # Phase 5: Monitor
    overall_pass = False
    try:
        results = wait_for_load_test(
            branch,
            pr_created_at,
            allocations,
            timeout_minutes=args.timeout,
        )
        overall_pass = print_load_test_report(
            args.cluster_id,
            cluster_name,
            results,
        )
    except KeyboardInterrupt:
        log.warning("Interrupted! Collecting partial results...")
        try:
            results = wait_for_load_test(
                branch,
                pr_created_at,
                allocations,
                timeout_minutes=1,  # short timeout for partial collection
            )
            overall_pass = print_load_test_report(
                args.cluster_id,
                cluster_name,
                results,
            )
        except KeyboardInterrupt:
            log.warning("Double interrupt — skipping result collection.")
    finally:
        if not args.keep_pr:
            log.info("Closing PR #%d...", pr_number)
            run_cmd(
                ["gh", "pr", "close", str(pr_number), "--repo", CANARY_REPO, "--delete-branch"],
                check=False,
            )
        else:
            log.info("Keeping PR #%d open (--keep-pr).", pr_number)

    sys.exit(0 if overall_pass else 1)


def _prepare_load_test_pr(
    canary_path: Path,
    upstream_dir: Path,
    workflow_content: str,
    branch: str,
) -> int | None:
    """Create the load test PR on pytorch-canary. Returns PR number."""
    log.info("Preparing load test PR...")

    run_cmd(["git", "fetch", "origin", "main"], cwd=canary_path)
    run_cmd(["git", "checkout", "-B", branch, "origin/main"], cwd=canary_path)

    # Write workflow
    workflows_dir = canary_path / ".github" / "workflows"
    if workflows_dir.exists():
        for f in workflows_dir.iterdir():
            if f.is_file():
                f.unlink()
    else:
        workflows_dir.mkdir(parents=True, exist_ok=True)

    (workflows_dir / "load-test.yaml").write_text(workflow_content)

    # Commit
    run_cmd(["git", "add", "-A"], cwd=canary_path)
    result = run_cmd(
        ["git", "diff", "--cached", "--quiet"],
        cwd=canary_path,
        check=False,
    )
    if result.returncode != 0:
        now = datetime.now(tz=UTC).strftime("%Y-%m-%d %H:%M UTC")
        run_cmd(
            ["git", "commit", "-m", f"{LOAD_TEST_PR_TITLE_PREFIX} {now}"],
            cwd=canary_path,
        )

    # Push
    run_cmd(["git", "push", "-f", "origin", branch], cwd=canary_path)

    # Open PR
    now = datetime.now(tz=UTC).strftime("%Y-%m-%d %H:%M")
    result = run_cmd(
        [
            "gh",
            "pr",
            "create",
            "--repo",
            CANARY_REPO,
            "--title",
            f"{LOAD_TEST_PR_TITLE_PREFIX} {now}",
            "--body",
            "Automated load test from OSDC. Do not review or merge.",
            "--head",
            branch,
            "--base",
            "main",
        ]
    )
    pr_url = result.stdout.strip()
    pr_number = int(pr_url.rstrip("/").split("/")[-1])
    log.info("PR #%d created: %s", pr_number, pr_url)
    return pr_number


if __name__ == "__main__":
    main()
