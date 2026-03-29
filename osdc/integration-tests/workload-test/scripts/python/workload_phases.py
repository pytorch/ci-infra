"""Phase functions for the OSDC workload test orchestrator.

Phase 1: Prepare repos (clone/update pytorch/pytorch + canary)
Phase 2: Mirror content (git archive | tar)
Phase 3: Instrument workflows (filter jobs, rewrite refs, prefix replacement)
Phase 4: Create PR on pytorch-canary
Phase 5: Monitor workflow runs
Phase 6: Report results
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

from run import CANARY_REPO, run_cmd, safe_json_loads
from workload_instrument import (
    SOURCE_RUNNER_PREFIX,
    _is_entry_point_workflow,
    apply_to_all_workflows,
    filter_non_arc_jobs,
    generate_determinator_script,
    generate_determinator_stub,
    inject_pypi_cache_step,
    replace_runner_prefix,
    rewrite_cross_repo_refs,
    rewrite_repo_guards,
)

log = logging.getLogger("osdc-workload-test")

# ── Constants ─────────────────────────────────────────────────────────

PYTORCH_REPO = "pytorch/pytorch"
KEEP_WORKFLOWS = ["lint.yml"]
PR_TITLE_PREFIX = "[NO REVIEW][NO MERGE] OSDC workload test"
SCRATCH_DIR_NAME = ".scratch"
# TODO: Move to @main once pytorch/test-infra PR is merged
PYPI_CACHE_ACTION_REF = "pytorch/test-infra/.github/actions/setup-pypi-cache@jeanschmidt/define_pip_cuda"


# ── Phase 1: Prepare repos ───────────────────────────────────────────


def ensure_pytorch_repo(upstream_dir: Path) -> Path:
    """Clone or update ``pytorch/pytorch`` into ``.scratch/`` at ``viable/strict``."""
    scratch = upstream_dir / SCRATCH_DIR_NAME
    scratch.mkdir(parents=True, exist_ok=True)

    pytorch_path = scratch / "pytorch"
    if pytorch_path.exists():
        check = run_cmd(["git", "rev-parse", "--git-dir"], cwd=pytorch_path, check=False)
        if check.returncode != 0:
            log.warning("pytorch repo at %s appears corrupt, re-cloning...", pytorch_path)
            shutil.rmtree(pytorch_path)
        else:
            log.info("  pytorch repo already cloned, fetching...")
            run_cmd(["git", "fetch", "origin"], capture=False, cwd=pytorch_path)

    if not pytorch_path.exists():
        log.info("  Cloning %s (blobless)...", PYTORCH_REPO)
        run_cmd(
            [
                "git",
                "clone",
                "--filter=blob:none",
                "--no-recurse-submodules",
                f"https://github.com/{PYTORCH_REPO}.git",
                str(pytorch_path),
            ],
            capture=False,
        )

    # Check out viable/strict (latest all-green commit)
    log.info("  Checking out viable/strict...")
    result = run_cmd(["git", "checkout", "origin/viable/strict"], cwd=pytorch_path, check=False)
    if result.returncode != 0:
        log.warning("  viable/strict not available, falling back to main")
        run_cmd(["git", "checkout", "origin/main"], cwd=pytorch_path)

    _check_freshness(pytorch_path)
    return pytorch_path


def _check_freshness(pytorch_path: Path) -> None:
    """Warn if the checked-out ref is more than 24 hours old."""
    result = run_cmd(["git", "log", "-1", "--format=%ct"], cwd=pytorch_path, check=False)
    if result.returncode != 0 or not result.stdout.strip():
        return
    age_hours = (time.time() - int(result.stdout.strip())) / 3600
    if age_hours > 24:
        log.warning("  viable/strict is %.0f hours old — results may be stale", age_hours)


# ── Phase 2: Mirror content ──────────────────────────────────────────


def mirror_content(pytorch_path: Path, canary_path: Path) -> None:
    """Mirror pytorch content into canary using ``git archive | tar``.

    Deletes everything in *canary_path* except ``.git/``, then extracts
    tracked files from *pytorch_path*.  Submodule gitlinks are skipped
    (submodule directories appear empty).
    """
    log.info("  Clearing canary working tree...")
    for item in canary_path.iterdir():
        if item.name == ".git":
            continue
        if item.is_dir():
            shutil.rmtree(item)
        else:
            item.unlink()

    log.info("  Extracting pytorch content via git archive...")
    archive = subprocess.Popen(
        ["git", "archive", "HEAD"],
        cwd=pytorch_path,
        stdout=subprocess.PIPE,
    )
    tar = subprocess.Popen(
        ["tar", "-x", "-C", str(canary_path)],
        stdin=archive.stdout,
    )
    archive.stdout.close()
    tar.communicate()
    if archive.wait() != 0:
        raise RuntimeError("git archive failed")
    if tar.returncode != 0:
        raise RuntimeError("tar extraction failed")


# ── Phase 3: Instrument workflows ────────────────────────────────────


def instrument_workflows(
    canary_path: Path,
    target_prefix: str,
    keep_workflows: list[str] | None = None,
) -> None:
    """Run all instrumentation steps on the mirrored canary repo."""
    if keep_workflows is None:
        keep_workflows = list(KEEP_WORKFLOWS)

    workflows_dir = canary_path / ".github" / "workflows"
    if not workflows_dir.exists():
        log.warning("  No .github/workflows/ directory found")
        return

    # 3a: Remove unwanted entry-point workflows
    log.info("  3a: Removing unwanted entry-point workflows...")
    for wf in sorted(workflows_dir.iterdir()):
        if not wf.name.endswith((".yml", ".yaml")):
            continue
        if _is_entry_point_workflow(wf) and wf.name not in keep_workflows:
            log.debug("    Removing entry-point: %s", wf.name)
            wf.unlink()

    # 3b: Remove non-ARC jobs from kept entry-point workflows
    log.info("  3b: Filtering non-ARC jobs...")
    for wf_name in keep_workflows:
        wf = workflows_dir / wf_name
        if wf.exists():
            content = wf.read_text()
            modified = filter_non_arc_jobs(content)
            if modified != content:
                wf.write_text(modified)

    # 3c: Rewrite cross-repo references in ALL remaining files
    log.info("  3c: Rewriting cross-repo references...")
    apply_to_all_workflows(workflows_dir, rewrite_cross_repo_refs)

    # 3d: Rewrite repository guards in ALL remaining files
    log.info("  3d: Rewriting repository guards...")
    apply_to_all_workflows(workflows_dir, rewrite_repo_guards)

    # 3e: Write determinator stub + script
    log.info("  3e: Writing determinator stub...")
    (workflows_dir / "_runner-determinator.yml").write_text(generate_determinator_stub(target_prefix))
    scripts_dir = canary_path / ".github" / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    (scripts_dir / "runner_determinator.py").write_text(generate_determinator_script(target_prefix))

    # 3f: Replace runner label prefixes in ALL remaining files
    log.info("  3f: Replacing runner label prefixes (%s -> %s)...", SOURCE_RUNNER_PREFIX, target_prefix)
    apply_to_all_workflows(
        workflows_dir,
        lambda c: replace_runner_prefix(c, SOURCE_RUNNER_PREFIX, target_prefix),
    )

    # 3g: Inject PyPI cache setup into all workflows with steps
    log.info("  3g: Injecting PyPI cache setup (auto-detect from build-environment)...")
    apply_to_all_workflows(
        workflows_dir,
        lambda c: inject_pypi_cache_step(c, PYPI_CACHE_ACTION_REF),
    )


# ── Phase 4: Create PR ───────────────────────────────────────────────


def create_workload_pr(
    canary_path: Path,
    cluster_id: str,
    dry_run: bool,
    branch: str,
) -> int | None:
    """Commit, push, and open a PR on pytorch-canary.  Returns PR number."""
    log.info("Phase 4: Creating PR...")

    run_cmd(["git", "config", "user.name", "OSDC Workload Test"], cwd=canary_path, capture=False, check=False)
    run_cmd(
        ["git", "config", "user.email", "osdc-workload-test@pytorch.org"],
        cwd=canary_path,
        capture=False,
        check=False,
    )

    run_cmd(["git", "add", "-A"], cwd=canary_path)
    if run_cmd(["git", "diff", "--cached", "--quiet"], cwd=canary_path, check=False).returncode == 0:
        log.info("  No changes to commit.")
        return None

    now = datetime.now(tz=UTC).strftime("%Y-%m-%d %H:%M UTC")
    run_cmd(["git", "commit", "-m", f"{PR_TITLE_PREFIX} — {cluster_id} — {now}"], cwd=canary_path)

    if dry_run:
        log.info("  DRY RUN: would push to %s and open PR.", branch)
        return None

    run_cmd(["git", "push", "-f", "origin", branch], cwd=canary_path)

    # Try to create a new PR; if one already exists for this branch,
    # gh pr create fails — fall back to finding the existing PR.
    result = run_cmd(
        [
            "gh",
            "pr",
            "create",
            "--repo",
            CANARY_REPO,
            "--title",
            f"{PR_TITLE_PREFIX} — {cluster_id}",
            "--body",
            "Automated workload test from OSDC. Do not review or merge.",
            "--head",
            branch,
            "--base",
            "main",
        ],
        check=False,
    )

    if result.returncode != 0:
        log.info("  gh pr create failed, checking for existing PR on branch '%s'...", branch)
        view = run_cmd(
            ["gh", "pr", "view", "--repo", CANARY_REPO, branch, "--json", "number,url"],
            check=False,
        )
        if view.returncode == 0:
            pr_data = safe_json_loads(view.stdout, "pr view")
            if pr_data and "number" in pr_data:
                pr_number = pr_data["number"]
                log.info("  Reusing existing PR #%d: %s", pr_number, pr_data.get("url", ""))
                return pr_number
        log.error("  Could not create or find PR: %s", result.stderr.strip())
        return None

    pr_url = result.stdout.strip()
    try:
        pr_number = int(pr_url.rstrip("/").split("/")[-1])
    except (ValueError, IndexError):
        log.error("  Could not parse PR number from: %s", pr_url)
        return None
    log.info("  PR #%d created: %s", pr_number, pr_url)
    return pr_number


# ── Phase 5: Monitor workflows ───────────────────────────────────────


def monitor_workflows(
    branch: str,
    pr_created_at: datetime,
    timeout_minutes: int = 60,
    poll_interval: int = 30,
) -> list[dict]:
    """Poll for workflow completion and collect results."""
    from phases_validation import _collect_run_details, _filter_runs_by_time

    log.info("Phase 5: Monitoring workflows (timeout: %d min)...", timeout_minutes)

    deadline = time.time() + timeout_minutes * 60
    logged_ids: set[int] = set()

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
            time.sleep(poll_interval)
            continue

        all_runs = safe_json_loads(result.stdout, "list runs") or []
        runs = _filter_runs_by_time(all_runs, pr_created_at)

        if not runs:
            log.info("  No runs found yet, waiting...")
            time.sleep(poll_interval)
            continue

        for r in runs:
            rid = r.get("databaseId")
            if rid and rid not in logged_ids:
                logged_ids.add(rid)
                url = f"https://github.com/{CANARY_REPO}/actions/runs/{rid}"
                log.info("  Run: %s — %s", r.get("name", "?"), url)

        if all(r.get("status") == "completed" for r in runs):
            log.info("  All %d run(s) completed.", len(runs))
            return _collect_run_details(runs)

        pending = sum(1 for r in runs if r.get("status") != "completed")
        log.info("  %d/%d runs in progress...", pending, len(runs))
        time.sleep(poll_interval)

    log.warning("  Timeout reached — collecting partial results.")
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
        all_runs = safe_json_loads(result.stdout, "fetch latest") or []
        return _collect_run_details(_filter_runs_by_time(all_runs, pr_created_at))
    return []


# ── Phase 6: Report ──────────────────────────────────────────────────


def print_workload_report(
    cluster_id: str,
    cluster_name: str,
    workflow_results: list[dict],
    interrupted: bool = False,
) -> bool:
    """Print summary report.  Returns True if all jobs passed."""
    overall_pass = True
    title = "OSDC Workload Test Results"
    if interrupted:
        title += " (interrupted)"

    now = datetime.now(tz=UTC).strftime("%Y-%m-%d %H:%M UTC")

    print("\n")
    print("=" * 60)
    print(f"  {title}")
    print("=" * 60)
    print(f"  Cluster: {cluster_id} ({cluster_name})")
    print(f"  Date:    {now}")
    print()

    if workflow_results:
        for run_info in workflow_results:
            for job in run_info.get("jobs", []):
                name = job.get("name", "unknown")
                conclusion = job.get("conclusion") or "in_progress"
                icon = "\u2713" if conclusion == "success" else ("\u2026" if conclusion == "in_progress" else "\u2717")
                if conclusion not in ("success", "in_progress"):
                    overall_pass = False
                print(f"    {icon} {name:40s} {conclusion}")
            if run_info.get("failure_log"):
                print(f"    --- Failure log (run {run_info['run_id']}) ---")
                for ln in run_info["failure_log"].split("\n")[:20]:
                    print(f"    | {ln}")
                print("    ---")
        print()
    else:
        print("  No workflow results available.")
        print()

    print(f"  Overall: {'PASSED' if overall_pass else 'FAILED'}")
    print("=" * 60)
    print()
    return overall_pass
