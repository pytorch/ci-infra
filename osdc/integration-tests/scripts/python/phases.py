"""Phase functions for the OSDC integration test orchestrator (phases 0-2).

Phase 0: Cleanup stale PRs
Phase 1: Staging pool clear
Phase 2: Generate workflow + prepare PR
"""

import json
import logging
import time
from datetime import UTC, datetime
from pathlib import Path

from run import (
    CANARY_REPO,
    PR_TITLE_PREFIX,
    run_cmd,
)

log = logging.getLogger("osdc-integration-test")

SCRATCH_DIR_NAME = ".scratch"


def ensure_canary_repo(upstream_dir: Path) -> Path:
    """Clone or update pytorch-canary into .scratch/ and return its path."""
    scratch = upstream_dir / SCRATCH_DIR_NAME
    scratch.mkdir(parents=True, exist_ok=True)

    canary_path = scratch / "pytorch-canary"
    if canary_path.exists():
        log.info("  Canary repo already cloned at %s, fetching...", canary_path)
        run_cmd(["git", "fetch", "origin"], capture=False, cwd=canary_path)
    else:
        log.info("  Cloning %s into %s...", CANARY_REPO, canary_path)
        run_cmd([
            "gh", "repo", "clone", CANARY_REPO, str(canary_path),
            "--", "--filter=blob:none",
        ], capture=False)

    return canary_path


# ── Phase 0: Cleanup ───────────────────────────────────────────────────


def cleanup_stale_prs(branch: str, pr_title_prefix: str = PR_TITLE_PREFIX):
    """Close any stale integration test PRs and cancel running workflows."""
    log.info("Phase 0: Cleaning up stale PRs...")

    # Find open PRs matching our title pattern
    result = run_cmd(
        ["gh", "pr", "list", "--repo", CANARY_REPO, "--author", "@me",
         "--state", "open", "--json", "number,title"],
        check=False,
    )
    if result.returncode != 0:
        log.warning("Could not list PRs: %s", result.stderr.strip())
        return

    prs = json.loads(result.stdout) if result.stdout.strip() else []
    for pr in prs:
        if pr_title_prefix in pr.get("title", ""):
            log.info("  Closing stale PR #%d: %s", pr["number"], pr["title"])
            run_cmd(
                ["gh", "pr", "close", str(pr["number"]), "--repo", CANARY_REPO, "--delete-branch"],
                check=False,
            )

    # Cancel any running workflows on our branch
    result = run_cmd(
        ["gh", "run", "list", "--repo", CANARY_REPO, "--branch", branch,
         "--status", "queued", "--json", "databaseId"],
        check=False,
    )
    if result.returncode == 0 and result.stdout.strip():
        runs = json.loads(result.stdout)
        for r in runs:
            log.info("  Cancelling queued run %s", r["databaseId"])
            run_cmd(["gh", "run", "cancel", str(r["databaseId"]), "--repo", CANARY_REPO], check=False)

    result = run_cmd(
        ["gh", "run", "list", "--repo", CANARY_REPO, "--branch", branch,
         "--status", "in_progress", "--json", "databaseId"],
        check=False,
    )
    if result.returncode == 0 and result.stdout.strip():
        runs = json.loads(result.stdout)
        for r in runs:
            log.info("  Cancelling in-progress run %s", r["databaseId"])
            run_cmd(["gh", "run", "cancel", str(r["databaseId"]), "--repo", CANARY_REPO], check=False)


# ── Phase 1: Staging pool clear (arc-staging only) ─────────────────────


def clear_staging_pools(cluster_id: str, force: bool = False):
    """Clear karpenter nodepools and runner pods for staging. Only for arc-staging."""
    if cluster_id != "arc-staging":
        return

    log.info("Phase 1: Checking for active runner pods (arc-staging only)...")
    result = run_cmd(
        ["kubectl", "get", "pods", "-n", "arc-runners", "--no-headers"],
        check=False,
    )
    if result.returncode != 0:
        log.warning("  Could not check runner pods: %s", result.stderr.strip())
        return

    pod_lines = [line for line in result.stdout.strip().split("\n") if line.strip()]
    if not pod_lines:
        log.info("  No runner pods active. Skipping pool clear.")
        return

    log.info("  %d runner pod(s) active.", len(pod_lines))
    if not force:
        answer = input(f"  {len(pod_lines)} runner pods active. Cancel and drain? [y/N] ").strip().lower()
        if answer != "y":
            log.info("  Skipping pool clear.")
            return

    log.info("  Deleting runner pods...")
    run_cmd(["kubectl", "delete", "pods", "-n", "arc-runners", "--all", "--wait=false"], check=False)

    log.info("  Deleting karpenter nodepools...")
    run_cmd(["kubectl", "delete", "nodepools", "-l", "osdc.io/module=nodepools"], check=False)

    log.info("  Waiting for nodes to drain (up to 5 minutes)...")
    for _ in range(30):
        result = run_cmd(
            ["kubectl", "get", "nodes", "-l", "karpenter.sh/nodepool", "--no-headers"],
            check=False,
        )
        if not result.stdout.strip():
            break
        time.sleep(10)

    log.info("  Re-deploying nodepools...")
    run_cmd(["just", "deploy-module", "arc-staging", "nodepools"], check=False)


# ── Phase 2: Prepare PR ────────────────────────────────────────────────


def generate_workflow(upstream_dir: Path, prefix: str, cluster_id: str, cluster_name: str, b200_enabled: bool) -> str:
    """Generate the integration test workflow from template."""
    template_path = upstream_dir / "integration-tests" / "workflows" / "integration-test.yaml.tpl"
    content = template_path.read_text()

    # Substitute template variables
    content = content.replace("{{PREFIX}}", prefix)
    content = content.replace("{{CLUSTER_ID}}", cluster_id)
    content = content.replace("{{CLUSTER_NAME}}", cluster_name)

    # Handle B200 conditional blocks
    if not b200_enabled:
        lines = content.split("\n")
        filtered = []
        in_b200_block = False
        for line in lines:
            stripped = line.strip()
            if stripped == "# BEGIN_B200":
                in_b200_block = True
                continue
            if stripped == "# END_B200":
                in_b200_block = False
                continue
            if not in_b200_block:
                filtered.append(line)
        content = "\n".join(filtered)
    else:
        # Remove the marker comments but keep the content
        content = content.replace("  # BEGIN_B200\n", "")
        content = content.replace("  # END_B200\n", "")

    return content


def prepare_pr(
    canary_path: Path,
    upstream_dir: Path,
    workflow_content: str,
    dry_run: bool = False,
    branch: str = "osdc-integration-test",
) -> int | None:
    """Create a branch, add workflow files, push, and open a PR. Returns PR number."""
    log.info("Phase 2: Preparing PR...")

    # Fetch and create branch
    run_cmd(["git", "fetch", "origin", "main"], cwd=canary_path)
    run_cmd(["git", "checkout", "-B", branch, "origin/main"], cwd=canary_path)

    # Clean existing workflows
    workflows_dir = canary_path / ".github" / "workflows"
    if workflows_dir.exists():
        for f in workflows_dir.iterdir():
            f.unlink()
    else:
        workflows_dir.mkdir(parents=True, exist_ok=True)

    # Write integration test workflow
    (workflows_dir / "integration-test.yaml").write_text(workflow_content)

    # Copy build-image reusable workflow
    build_wf_src = upstream_dir / "integration-tests" / "workflows" / "build-image.yaml"
    (workflows_dir / "build-image.yaml").write_text(build_wf_src.read_text())

    # Copy test Dockerfile
    docker_dir = canary_path / "docker" / "test-buildkit"
    docker_dir.mkdir(parents=True, exist_ok=True)
    dockerfile_src = upstream_dir / "integration-tests" / "docker" / "test-buildkit" / "Dockerfile"
    (docker_dir / "Dockerfile").write_text(dockerfile_src.read_text())

    # Commit
    run_cmd(["git", "add", "-A"], cwd=canary_path)

    # Check if there are changes to commit
    result = run_cmd(["git", "diff", "--cached", "--quiet"], cwd=canary_path, check=False)
    if result.returncode == 0:
        log.info("  No changes to commit.")
    else:
        now = datetime.now(tz=UTC).strftime("%Y-%m-%d %H:%M UTC")
        run_cmd(
            ["git", "commit", "-m", f"{PR_TITLE_PREFIX} {now}"],
            cwd=canary_path,
        )

    if dry_run:
        log.info("  DRY RUN: Would push to %s and open PR.", branch)
        log.info("  Generated workflow written to %s", workflows_dir / "integration-test.yaml")
        return None

    # Push
    run_cmd(["git", "push", "-f", "origin", branch], cwd=canary_path)

    # Open PR
    now = datetime.now(tz=UTC).strftime("%Y-%m-%d %H:%M")
    result = run_cmd([
        "gh", "pr", "create",
        "--repo", CANARY_REPO,
        "--title", f"{PR_TITLE_PREFIX} {now}",
        "--body", "Automated integration test from OSDC. Do not review or merge.",
        "--head", branch,
        "--base", "main",
    ])
    # Extract PR number from URL
    pr_url = result.stdout.strip()
    pr_number = int(pr_url.rstrip("/").split("/")[-1])
    log.info("  PR #%d created: %s", pr_number, pr_url)
    return pr_number
