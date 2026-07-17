"""Phase functions for the OSDC integration test orchestrator (phases 0-2).

Phase 0: Cleanup stale PRs
Phase 1: Staging pool clear
Phase 2: Generate workflow + prepare PR
"""

import logging
import re
import shutil
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts" / "python"))
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "modules" / "arc-runners" / "scripts" / "python"))

from conditional_blocks import strip_conditional_block
from generate_runners import parse_memory_bytes
from nodepool_defs import is_excluded_for_region
from run import (
    CANARY_REPO,
    PR_TITLE_PREFIX,
    is_prod_cluster,
    normalize_modules,
    run_cmd,
    safe_json_loads,
)

log = logging.getLogger("osdc-integration-test")

SCRATCH_DIR_NAME = ".scratch"

TAG_REQUIREMENTS: dict[str, list[str]] = {
    "ARC_RUNNERS": ["arc-runners"],
    "PYPI_CACHE": ["arc-runners", "pypi-cache"],
    "HF_CACHE": ["arc-runners", "hf-cache"],
    "HF_CACHE_OIDC": ["arc-runners", "hf-cache"],
    "GPU_T4": ["arc-runners", "nodepools"],
    "BUILDKIT": ["arc-runners", "buildkit"],
    "CACHE_ENFORCER": ["arc-runners", "cache-enforcer"],
    "RELEASE": ["arc-runners"],
}

INVERSE_TAG_EXCLUSIONS: dict[str, list[str]] = {
    "NO_CACHE_ENFORCER": ["cache-enforcer"],
}

# Blocks stripped on prod clusters. Prod release runner groups are restricted to
# selected pytorch workflows, so the canary integration-test PR can't schedule on
# them — the release tests run on staging only.
PROD_EXCLUDED_BLOCKS: set[str] = {"RELEASE"}

# Conditional blocks whose job targets a runner backed by a region-restricted
# GPU fleet. Module gating can't catch this — the fleet exists as a module but is
# excluded in some regions (exclude_regions in modules/nodepools/defs/<fleet>.yaml).
# Where the fleet is excluded the block is stripped, else the job queues forever
# for a runner that can never come online.
REGION_GATED_BLOCKS: dict[str, str] = {
    "HF_CACHE_GPU": "g6",  # test-hf-cache-large-read runs on an L4 (g6) runner
}


def region_excluded_blocks(upstream_dir: Path, region: str) -> set[str]:
    """Return REGION_GATED_BLOCKS tags whose backing fleet is excluded in *region*."""
    if not region:
        return set()
    excluded = set()
    for tag, fleet in REGION_GATED_BLOCKS.items():
        def_path = upstream_dir / "modules" / "nodepools" / "defs" / f"{fleet}.yaml"
        try:
            data = yaml.safe_load(def_path.read_text()) or {}
        except FileNotFoundError:
            continue
        fleet_def = data.get("fleet") or {}
        if is_excluded_for_region(fleet_def, region):
            excluded.add(tag)
    return excluded


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


def resource_placeholders(upstream_dir: Path, cluster_modules: list[str]) -> dict[str, str]:
    """Build resource-assertion placeholders from the cluster's arc-runners defs.

    For every runner def in each enabled arc-runners* module's defs/ dir, emit
    four placeholders keyed by the def name uppercased with hyphens as underscores
    (VCPU__/MEMGI__/TORCHMEM__/GPU__<KEY>). These feed the integration-test
    template's hardcoded CPU/memory/GPU assertions so they track the deployed
    runner spec instead of drifting.

    Base module defs are processed first, then specialized variants (-opt/-b200/
    -h100), so a variant that redefines a shared label wins.
    """
    base = [m for m in cluster_modules if m == "arc-runners"]
    specialized = sorted(m for m in cluster_modules if m.startswith("arc-runners") and m != "arc-runners")
    placeholders: dict[str, str] = {}
    for module in base + specialized:
        defs_dir = upstream_dir / "modules" / module / "defs"
        if not defs_dir.is_dir():
            continue
        for def_path in sorted(defs_dir.glob("*.yaml")):
            data = yaml.safe_load(def_path.read_text()) or {}
            runner = data.get("runner")
            if not runner:
                continue
            name = runner.get("name")
            if not name:
                continue
            key = name.upper().replace("-", "_")
            memory_bytes = parse_memory_bytes(runner["memory"])
            placeholders[f"{{{{VCPU__{key}}}}}"] = str(runner["vcpu"])
            placeholders[f"{{{{MEMGI__{key}}}}}"] = str(memory_bytes // (1024**3))
            placeholders[f"{{{{TORCHMEM__{key}}}}}"] = str(memory_bytes)
            placeholders[f"{{{{GPU__{key}}}}}"] = str(runner.get("gpu", 0))
    return placeholders


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
    ecr_pull_resolved_tag: str = "",
    ecr_pull_sha: str = "",
    region: str = "",
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
    content = content.replace("{{ECR_PULL_RESOLVED_TAG}}", ecr_pull_resolved_tag)
    content = content.replace("{{ECR_PULL_SHA}}", ecr_pull_sha)

    for placeholder, value in resource_placeholders(upstream_dir, cluster_modules).items():
        content = content.replace(placeholder, value)

    # Guard: any "{{...}}" left after substitution is a template/code drift bug.
    leftover = re.findall(r"\{\{[A-Z_]+\}\}", content)
    if leftover:
        raise RuntimeError(f"Unsubstituted template placeholders remain: {sorted(set(leftover))}")

    # Before module gating: these tags also live in TAG_REQUIREMENTS, whose loop
    # consumes their markers. On non-prod, leave them for module/region gating.
    if is_prod_cluster(cluster_id):
        for tag in PROD_EXCLUDED_BLOCKS:
            content = strip_conditional_block(content, tag, keep=False)

    modules_set = normalize_modules(cluster_modules)
    for tag, required in TAG_REQUIREMENTS.items():
        keep = all(m in modules_set for m in required)
        content = strip_conditional_block(content, tag, keep=keep)

    for tag, excluded in INVERSE_TAG_EXCLUSIONS.items():
        keep = not any(m in modules_set for m in excluded)
        content = strip_conditional_block(content, tag, keep=keep)

    # Region gate (after module gating, so a block already removed with its parent
    # is a no-op here): strip GPU test blocks whose fleet is excluded in this region,
    # and clear the markers from any that survive.
    excluded_blocks = region_excluded_blocks(upstream_dir, region)
    for tag in REGION_GATED_BLOCKS:
        content = strip_conditional_block(content, tag, keep=tag not in excluded_blocks)

    # Resource-assertion guard (after stripping, so placeholders inside a removed
    # job — e.g. b200 on a non-b200 cluster — are already gone). A surviving
    # resource placeholder means a running job's runner def wasn't found: a real bug.
    leftover_res = re.findall(r"\{\{(?:VCPU|MEMGI|TORCHMEM|GPU)__[A-Z0-9_]+\}\}", content)
    if leftover_res:
        raise RuntimeError(f"Unresolved resource placeholders: {sorted(set(leftover_res))}")

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
