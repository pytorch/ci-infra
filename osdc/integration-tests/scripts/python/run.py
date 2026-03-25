#!/usr/bin/env python3
"""OSDC Integration Test Orchestrator.

Runs a full integration test against an OSDC cluster by:
1. Cleaning up stale PRs on pytorch/pytorch-canary
2. Optionally clearing staging pools (arc-staging only)
3. Opening a PR with test workflows that exercise every cluster capability
4. Running smoke tests and node-compactor tests in parallel
5. Collecting workflow results and reporting
6. Cleaning up
"""

import argparse
import json
import logging
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import yaml

log = logging.getLogger("osdc-integration-test")

CANARY_REPO = "pytorch/pytorch-canary"
PR_TITLE_PREFIX = "[NO REVIEW][NO MERGE] ARC smoke tests"
WORKFLOW_TIMEOUT_MINUTES = 60
POLL_INTERVAL_SECONDS = 30


def branch_name(cluster_id: str) -> str:
    """Return a cluster-specific branch name to avoid collisions between parallel runs."""
    return f"osdc-integration-test-{cluster_id}"


# ── Config helpers ──────────────────────────────────────────────────────


def load_cluster_config(clusters_yaml: Path, cluster_id: str) -> dict:
    """Load cluster config from clusters.yaml and return resolved config."""
    with open(clusters_yaml) as f:
        data = yaml.safe_load(f)

    defaults = data.get("defaults", {})
    clusters = data.get("clusters", {})
    if cluster_id not in clusters:
        log.error("Cluster '%s' not found in %s", cluster_id, clusters_yaml)
        sys.exit(1)

    return {"cluster": clusters[cluster_id], "defaults": defaults}


def resolve(cfg: dict, dotpath: str, default=None):
    """Resolve a dot-separated path against cluster config with defaults fallback."""
    parts = dotpath.split(".")
    # Try cluster config first
    val = cfg["cluster"]
    for part in parts:
        if isinstance(val, dict) and part in val:
            val = val[part]
        else:
            val = None
            break
    if val is not None:
        return val
    # Fall back to defaults
    val = cfg["defaults"]
    for part in parts:
        if isinstance(val, dict) and part in val:
            val = val[part]
        else:
            return default
    return val if val is not None else default


def has_module(cfg: dict, module_name: str) -> bool:
    """Check if a module is enabled for the cluster."""
    modules = cfg["cluster"].get("modules", [])
    return module_name in modules


# ── Subprocess helpers ──────────────────────────────────────────────────


def run_cmd(cmd: list[str], *, check: bool = True, capture: bool = True, **kwargs) -> subprocess.CompletedProcess:
    """Run a command with logging."""
    log.debug("Running: %s", " ".join(cmd))
    return subprocess.run(cmd, check=check, capture_output=capture, text=True, **kwargs)


def run_cmd_with_retry(
    cmd: list[str],
    *,
    max_retries: int = 3,
    base_delay: float = 5.0,
    **kwargs,
) -> subprocess.CompletedProcess:
    """Run a command with exponential backoff retry on failure."""
    last_err = None
    for attempt in range(max_retries):
        result = run_cmd(cmd, check=False, **kwargs)
        if result.returncode == 0:
            return result
        last_err = result
        if attempt < max_retries - 1:
            delay = base_delay * (2 ** attempt)
            log.warning(
                "Command failed (attempt %d/%d, exit %d), retrying in %.0fs: %s",
                attempt + 1, max_retries, result.returncode, delay, " ".join(cmd),
            )
            time.sleep(delay)
    log.warning(
        "Command failed after %d attempts (exit %d): %s",
        max_retries, last_err.returncode, " ".join(cmd),
    )
    return last_err


def safe_json_loads(text: str | None, context: str = "") -> dict | list | None:
    """Parse JSON with error handling. Returns None on parse failure."""
    if not text or not text.strip():
        return None
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError) as e:
        ctx = f" ({context})" if context else ""
        log.warning("Failed to parse JSON%s: %s", ctx, e)
        log.debug("Raw content: %s", text[:500])
        return None


def gh_api(endpoint: str, method: str = "GET", **kwargs) -> dict | list | None:
    """Call GitHub API via gh CLI."""
    cmd = ["gh", "api", endpoint, "--method", method]
    for key, val in kwargs.items():
        cmd.extend(["-f", f"{key}={val}"])
    result = run_cmd(cmd, check=False)
    if result.returncode != 0:
        log.warning("gh api %s failed: %s", endpoint, result.stderr.strip())
        return None
    return safe_json_loads(result.stdout, context=f"gh api {endpoint}")


def format_duration(seconds: float) -> str:
    """Format seconds into human-readable duration."""
    total = int(round(seconds))
    if total < 60:
        return f"{total}s"
    return f"{total // 60}m{total % 60:02d}s"


# ── Main ────────────────────────────────────────────────────────────────


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="OSDC Integration Test Orchestrator")
    parser.add_argument("--cluster-id", required=True, help="Cluster ID from clusters.yaml")
    parser.add_argument("--clusters-yaml", required=True, type=Path, help="Path to clusters.yaml")
    parser.add_argument("--upstream-dir", required=True, type=Path, help="OSDC upstream directory")
    parser.add_argument("--root-dir", required=True, type=Path, help="OSDC root directory (consumer or upstream)")
    parser.add_argument("--skip-smoke", action="store_true", help="Skip smoke tests")
    parser.add_argument("--skip-compactor", action="store_true", help="Skip node-compactor e2e tests")
    parser.add_argument("--dry-run", action="store_true", help="Generate workflows but don't push/PR")
    parser.add_argument("--keep-pr", action="store_true", help="Don't close PR after test (useful for debugging failures)")
    parser.add_argument("--force", action="store_true", help="Skip interactive prompts (e.g. staging pool clear)")
    parser.add_argument("--skip-drain", action="store_true", help="Skip staging pool drain entirely")
    return parser.parse_args()


def main():
    from phases import (
        cleanup_stale_prs,
        clear_staging_pools,
        ensure_canary_repo,
        generate_workflow,
        prepare_pr,
    )
    from phases_validation import (
        close_pr,
        print_report,
        run_parallel_validation,
        wait_for_workflows,
    )

    args = parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    cfg = load_cluster_config(args.clusters_yaml, args.cluster_id)
    cluster_name = resolve(cfg, "cluster_name")
    prefix = resolve(cfg, "arc-runners.runner_name_prefix", "")
    b200_enabled = has_module(cfg, "nodepools-b200") and has_module(cfg, "arc-runners-b200")
    branch = branch_name(args.cluster_id)

    log.info("Integration test for cluster: %s (%s)", args.cluster_id, cluster_name)
    log.info("  Runner prefix: '%s'", prefix)
    log.info("  B200 enabled: %s", b200_enabled)
    log.info("  Branch: %s", branch)

    start_time = time.monotonic()
    pr_number = None
    pr_created_at = None
    overall_pass = False
    validation_results = {}
    workflow_results = []
    try:
        # Phase 0: Cleanup
        cleanup_stale_prs(branch)

        # Phase 1: Staging pool clear
        if not args.dry_run and not args.skip_drain:
            clear_staging_pools(args.cluster_id, force=args.force)

        # Clone / update canary repo
        canary_path = ensure_canary_repo(args.upstream_dir)

        # Phase 2: Prepare PR
        workflow_content = generate_workflow(
            args.upstream_dir, prefix, args.cluster_id, cluster_name, b200_enabled,
        )
        pr_created_at = datetime.now(tz=UTC)
        pr_number = prepare_pr(canary_path, args.upstream_dir, workflow_content, args.dry_run, branch)

        if args.dry_run:
            log.info("DRY RUN complete. No PR created.")
            sys.exit(0)

        # Phase 3: Parallel validation
        validation_results = run_parallel_validation(
            args.cluster_id, args.root_dir, args.upstream_dir,
            args.skip_smoke, args.skip_compactor, cfg,
        )

        # Phase 4: Collect workflow results
        workflow_results = wait_for_workflows(branch, pr_created_at)

        # Phase 5: Report
        overall_pass = print_report(
            args.cluster_id, cluster_name,
            workflow_results, validation_results,
        )

    except subprocess.CalledProcessError as e:
        log.error("Command failed with exit code %d: %s", e.returncode, " ".join(e.cmd))
        if e.stderr:
            log.error("stderr: %s", e.stderr.strip())
        if e.stdout:
            log.info("stdout: %s", e.stdout.strip())
        overall_pass = False

    except KeyboardInterrupt:
        log.warning("Interrupted! Printing available results...")

        # If we have a PR but no workflow results yet, try a quick fetch
        if pr_created_at and not workflow_results:
            from phases_validation import _collect_run_details, _fetch_latest_runs

            try:
                runs = _fetch_latest_runs(branch, pr_created_at)
                if runs:
                    workflow_results = _collect_run_details(runs)
            except KeyboardInterrupt:
                pass  # double Ctrl+C — skip fetch, print what we have

        overall_pass = print_report(
            args.cluster_id, cluster_name,
            workflow_results, validation_results,
            interrupted=True,
        )

    finally:
        if pr_number is not None and not args.keep_pr:
            close_pr(pr_number, branch=branch)
        elif pr_number is not None and args.keep_pr:
            log.info("Keeping PR #%d open (--keep-pr).", pr_number)

        elapsed = time.monotonic() - start_time
        log.info("Total integration test time: %s", format_duration(elapsed))

    sys.exit(0 if overall_pass else 1)


if __name__ == "__main__":
    main()
