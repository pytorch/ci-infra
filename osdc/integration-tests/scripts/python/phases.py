"""Phase functions for the OSDC integration test orchestrator (phases 0-2).

Phase 0: Cleanup stale PRs
Phase 1: Staging pool clear
Phase 2: Generate workflow + prepare PR
"""

import logging
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path

from run import (
    CANARY_REPO,
    PR_TITLE_PREFIX,
    run_cmd,
    safe_json_loads,
)

log = logging.getLogger("osdc-integration-test")

SCRATCH_DIR_NAME = ".scratch"

TAG_REQUIREMENTS: dict[str, list[str]] = {
    "ARC_RUNNERS": ["arc-runners"],
    "PYPI_CACHE": ["arc-runners", "pypi-cache"],
    "GPU_T4": ["arc-runners", "nodepools"],
    "BUILDKIT": ["arc-runners", "buildkit"],
    "B200": ["arc-runners-b200", "nodepools-b200"],
    "CACHE_ENFORCER": ["arc-runners", "cache-enforcer"],
    "RELEASE": ["arc-runners"],
}


def ensure_canary_repo(upstream_dir: Path) -> Path:
    """Clone or update pytorch-canary into .scratch/ and return its path."""
    # Register gh as git's credential helper so raw git commands (fetch, push)
    # can authenticate using GH_TOKEN — gh repo clone alone doesn't set this up.
    auth_result = run_cmd(["gh", "auth", "setup-git"], check=False)
    if auth_result.returncode != 0:
        log.warning(
            "gh auth setup-git failed (exit %d): %s",
            auth_result.returncode,
            auth_result.stderr.strip() if auth_result.stderr else "(no output)",
        )

    scratch = upstream_dir / SCRATCH_DIR_NAME
    scratch.mkdir(parents=True, exist_ok=True)

    canary_path = scratch / "pytorch-canary"
    if canary_path.exists():
        # Verify repo integrity before fetching
        check = run_cmd(["git", "rev-parse", "--git-dir"], cwd=canary_path, check=False)
        if check.returncode != 0:
            log.warning("  Canary repo at %s appears corrupt, re-cloning...", canary_path)
            shutil.rmtree(canary_path)
            # Fall through to clone path below
        else:
            # Remove stale git locks that can block fetch/push
            lock_file = canary_path / ".git" / "index.lock"
            if lock_file.exists():
                log.warning("  Removing stale git lock: %s", lock_file)
                lock_file.unlink()

            log.info("  Canary repo already cloned at %s, fetching...", canary_path)
            run_cmd(["git", "fetch", "origin"], capture=False, cwd=canary_path)

    if not canary_path.exists():
        log.info("  Cloning %s into %s...", CANARY_REPO, canary_path)
        run_cmd(
            [
                "gh",
                "repo",
                "clone",
                CANARY_REPO,
                str(canary_path),
                "--",
                "--filter=blob:none",
            ],
            capture=False,
        )

    # Configure git identity for commits made by the test orchestrator
    run_cmd(["git", "config", "user.name", "OSDC Integration Test"], cwd=canary_path, capture=False, check=False)
    run_cmd(
        ["git", "config", "user.email", "osdc-integration-test@pytorch.org"],
        cwd=canary_path,
        capture=False,
        check=False,
    )

    return canary_path


# ── Phase 0: Cleanup ───────────────────────────────────────────────────


def cleanup_stale_prs(branch: str, pr_title_prefix: str = PR_TITLE_PREFIX):
    """Close any stale integration test PRs and cancel running workflows."""
    log.info("Phase 0: Cleaning up stale PRs...")

    # Find open PRs matching our title pattern
    result = run_cmd(
        ["gh", "pr", "list", "--repo", CANARY_REPO, "--state", "open", "--json", "number,title"],
        check=False,
    )
    if result.returncode != 0:
        log.warning("Could not list PRs: %s", result.stderr.strip())
        return

    prs = safe_json_loads(result.stdout, "list PRs") or []
    for pr in prs:
        if pr_title_prefix in pr.get("title", ""):
            log.info("  Closing stale PR #%d: %s", pr["number"], pr["title"])
            run_cmd(
                ["gh", "pr", "close", str(pr["number"]), "--repo", CANARY_REPO, "--delete-branch"],
                check=False,
            )

    # Cancel any running workflows on our branch
    result = run_cmd(
        ["gh", "run", "list", "--repo", CANARY_REPO, "--branch", branch, "--status", "queued", "--json", "databaseId"],
        check=False,
    )
    if result.returncode == 0 and result.stdout.strip():
        runs = safe_json_loads(result.stdout, "list queued runs") or []
        for r in runs:
            log.info("  Cancelling queued run %s", r["databaseId"])
            run_cmd(["gh", "run", "cancel", str(r["databaseId"]), "--repo", CANARY_REPO], check=False)

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
            "in_progress",
            "--json",
            "databaseId",
        ],
        check=False,
    )
    if result.returncode == 0 and result.stdout.strip():
        runs = safe_json_loads(result.stdout, "list in-progress runs") or []
        for r in runs:
            log.info("  Cancelling in-progress run %s", r["databaseId"])
            run_cmd(["gh", "run", "cancel", str(r["databaseId"]), "--repo", CANARY_REPO], check=False)


# ── Phase 1: Staging pool clear (meta-staging-aws-uw1 only) ─────────────────────


def clear_staging_pools(cluster_id: str, force: bool = False):
    """Clear karpenter nodepools and runner pods for staging. Only for meta-staging-aws-uw1."""
    if cluster_id != "meta-staging-aws-uw1":
        return

    log.info("Phase 1: Checking for active runner pods (meta-staging-aws-uw1 only)...")
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
    run_cmd(["just", "deploy-module", "meta-staging-aws-uw1", "nodepools"], check=False)


# ── Phase 2: Prepare PR ────────────────────────────────────────────────


def _strip_conditional_block(content: str, tag: str, keep: bool) -> str:
    """Remove or keep a # BEGIN_<tag> / # END_<tag> conditional block.

    When *keep* is False the block (markers + content) is stripped entirely.
    When *keep* is True the content is kept but the marker comments are removed.
    """
    begin = f"# BEGIN_{tag}"
    end = f"# END_{tag}"
    if not keep:
        lines = content.split("\n")
        filtered = []
        inside = False
        for line in lines:
            stripped = line.strip()
            if stripped == begin:
                inside = True
                continue
            if stripped == end:
                inside = False
                continue
            if not inside:
                filtered.append(line)
        return "\n".join(filtered)
    # keep=True — remove markers, keep content
    content = content.replace(f"  {begin}\n", "")
    content = content.replace(f"  {end}\n", "")
    return content


def _has_any_job(content: str) -> bool:
    lines = content.split("\n")
    in_jobs = False
    for line in lines:
        if not in_jobs:
            if line.rstrip() == "jobs:":
                in_jobs = True
            continue
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if line.startswith("  ") and not line.startswith("   "):
            head = line[2:]
            if head and head[0].isalpha() and ":" in head:
                return True
    return False


def _replace_jobs_with_noop(content: str) -> str:
    lines = content.split("\n")
    out = []
    for line in lines:
        if line.rstrip() == "jobs:":
            out.append("jobs:")
            out.append("  no-op:")
            out.append("    runs-on: ubuntu-latest")
            out.append("    steps:")
            out.append('      - run: echo "No integration test suites match this cluster\'s modules"')
            break
        out.append(line)
    suffix = "\n" if content.endswith("\n") else ""
    return "\n".join(out) + suffix


def generate_workflow(
    upstream_dir: Path,
    prefix: str,
    cluster_id: str,
    cluster_name: str,
    cluster_modules: list[str],
    pypi_cache_slugs: str = "cpu cu121 cu124",
    pypi_cache_cuda_version: str = "12.8",
    runner_group: str = "default",
    release_runner_group: str = "release-runners",
) -> str:
    """Generate the integration test workflow from template."""
    template_path = upstream_dir / "integration-tests" / "workflows" / "integration-test.yaml.tpl"
    content = template_path.read_text()

    content = content.replace("{{PREFIX}}", prefix)
    content = content.replace("{{RUNNER_GROUP}}", runner_group)
    content = content.replace("{{RELEASE_RUNNER_GROUP}}", release_runner_group)
    content = content.replace("{{CLUSTER_ID}}", cluster_id)
    content = content.replace("{{CLUSTER_NAME}}", cluster_name)
    content = content.replace("{{PYPI_CACHE_SLUGS}}", pypi_cache_slugs)
    content = content.replace("{{PYPI_CACHE_CUDA_VERSION}}", pypi_cache_cuda_version)

    modules_set = set(cluster_modules)
    for tag, required in TAG_REQUIREMENTS.items():
        keep = all(m in modules_set for m in required)
        content = _strip_conditional_block(content, tag, keep=keep)

    if not _has_any_job(content):
        content = _replace_jobs_with_noop(content)

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

    # Clean existing workflows (rmtree handles subdirectories and symlinks safely)
    workflows_dir = canary_path / ".github" / "workflows"
    if workflows_dir.exists():
        shutil.rmtree(workflows_dir)
    workflows_dir.mkdir(parents=True, exist_ok=True)

    # Write integration test workflow
    (workflows_dir / "integration-test.yaml").write_text(workflow_content)

    # Copy the reusable BuildKit workflow (connectivity + autoscaling scale jobs).
    # The scale job builds an inline Dockerfile, so it needs no copied context.
    build_wf_src = upstream_dir / "integration-tests" / "workflows" / "build-image.yaml"
    (workflows_dir / "build-image.yaml").write_text(build_wf_src.read_text())

    # Copy test Dockerfile (connectivity test context)
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
    result = run_cmd(
        [
            "gh",
            "pr",
            "create",
            "--repo",
            CANARY_REPO,
            "--title",
            f"{PR_TITLE_PREFIX} {now}",
            "--body",
            "Automated integration test from OSDC. Do not review or merge.",
            "--head",
            branch,
            "--base",
            "main",
        ]
    )
    # Extract PR number from URL.
    # If parsing fails, return None — the caller (main()) will skip polling
    # and cleanup_stale_prs() will close the orphaned PR on the next run.
    pr_url = result.stdout.strip()
    try:
        pr_number = int(pr_url.rstrip("/").split("/")[-1])
    except (ValueError, IndexError):
        log.error("  Could not parse PR number from: %s", pr_url)
        log.error("  gh pr create stderr: %s", result.stderr.strip() if result.stderr else "(none)")
        return None
    log.info("  PR #%d created: %s", pr_number, pr_url)
    return pr_number
