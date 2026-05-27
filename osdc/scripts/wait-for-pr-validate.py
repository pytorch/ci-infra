#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# ///
"""Wait for the osdc/pr-validate commit status to flip to success or failure.

Used by osdc-renovate-autoapprove.yml to gate Renovate PR merges on a
staging validation that runs in osdc-pr-validate.yml. This script is the
"client side" of the gate: it dispatches the validation workflow (if no
status exists yet), polls the commit status, and emits a final decision
into $GITHUB_OUTPUT for the surrounding workflow.

Inputs (env vars, all required):
    GH_TOKEN     GitHub token with actions:write + statuses:read scope
    REPO         owner/repo (e.g. pytorch/ci-infra)
    PR_NUMBER    PR number (integer as string)
    HEAD_SHA     full 40-char SHA the autoapprover validated and will merge

Optional env vars:
    POLL_INTERVAL_SEC  default 90
    MAX_WAIT_SEC       default 21600 (6h)
    PENDING_GRACE_SEC  default 1800 (30 min) — if status is `pending` for
                       longer than this AND no osdc-pr-validate run is
                       in flight for HEAD_SHA, treat as stuck and
                       re-dispatch.
    GITHUB_OUTPUT      path to the per-step output file (set automatically
                       by GH Actions runner; required when run by a step)

Outputs (appended to $GITHUB_OUTPUT):
    decision=approve | close-head-moved | close-validation-failed |
             close-validation-timeout | close-dispatch-failed |
             close-branch-name-invalid
    reason=<human-readable explanation>

Hardening notes (each is load-bearing — see PR review):
    - Dispatch uses --ref main so the workflow FILE comes from a trusted
      branch. The PR's head_sha is passed as input so the validated CODE
      is still the PR head. A compromised PR-opening bot cannot swap the
      workflow file underneath us.
    - Re-reads head.sha on every poll. If the PR is force-pushed/rebased
      mid-wait, the old SHA's status is meaningless — bail with
      close-head-moved so Renovate reopens cleanly next cycle.
    - Defensive branch-name regex on head.ref before any gh calls that
      touch ref-shaped strings.
    - All gh api calls have bounded retry with backoff to absorb the
      occasional transient 5xx during a 6h wait.
    - Detects stuck `pending` (post job never reported terminal): if
      pending persists past PENDING_GRACE_SEC and no run is in-flight
      for HEAD_SHA, re-dispatches.
"""

import json
import os
import re
import subprocess
import sys
import time
from datetime import UTC
from typing import Any

# Strict shape we accept: only the canonical renovate-runner branch
# prefix with no leading-dash flag-injection risk.
HEAD_REF_PATTERN = re.compile(r"^renovate-runner/[A-Za-z0-9._/-]+$")

VALIDATE_WORKFLOW = "osdc-pr-validate.yml"
STATUS_CONTEXT = "osdc/pr-validate"

# Always dispatch the workflow at main so the workflow definition cannot
# be swapped by a malicious PR head.
DISPATCH_REF = "main"


def _env(name: str, default: str | None = None) -> str:
    v = os.environ.get(name, default)
    if v is None or v == "":
        sys.exit(f"missing required env var: {name}")
    return v


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        sys.exit(f"env var {name} must be an integer, got {raw!r}")


def _emit_output(decision: str, reason: str) -> None:
    """Append decision/reason to $GITHUB_OUTPUT for the surrounding step."""
    print(f"decision={decision}")
    print(f"reason={reason}")
    out_path = os.environ.get("GITHUB_OUTPUT")
    if out_path:
        with open(out_path, "a") as f:
            f.write(f"decision={decision}\n")
            f.write(f"reason={reason}\n")


def _gh_api(args: list[str], retries: int = 3, backoff: float = 5.0) -> str:
    """Run `gh api ARGS` with bounded retry. Returns stdout on success.

    Transient failures (network, GitHub 5xx) are swallowed up to `retries`
    attempts. Unrecoverable failures (auth, 4xx that isn't 404) raise.
    """
    last_err = ""
    for attempt in range(1, retries + 1):
        proc = subprocess.run(
            ["gh", "api", *args],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0:
            return proc.stdout
        last_err = proc.stderr.strip() or proc.stdout.strip()
        # Hard-fail on 4xx auth/permission errors — retry won't help.
        if any(m in last_err for m in ("401", "403", "Bad credentials", "Resource not accessible")):
            raise RuntimeError(f"gh api {args} failed (non-retryable): {last_err}")
        if attempt < retries:
            print(f"::warning::gh api transient failure (attempt {attempt}/{retries}): {last_err}")
            time.sleep(backoff * attempt)
    raise RuntimeError(f"gh api {args} exhausted retries: {last_err}")


def _gh_workflow_run(repo: str, workflow: str, ref: str, inputs: dict[str, str]) -> None:
    """Dispatch a workflow via gh CLI. Raises on failure."""
    args = [
        "workflow",
        "run",
        workflow,
        "--repo",
        repo,
        "--ref",
        ref,
    ]
    for k, v in inputs.items():
        args += ["-f", f"{k}={v}"]
    proc = subprocess.run(["gh", *args], capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"gh workflow run failed: {proc.stderr.strip() or proc.stdout.strip()}")


def _read_pr(repo: str, pr_number: str) -> dict[str, Any]:
    raw = _gh_api([f"repos/{repo}/pulls/{pr_number}"])
    return json.loads(raw)


def _read_status(repo: str, sha: str) -> tuple[str, float]:
    """Return (state, age_seconds) for the osdc/pr-validate status on `sha`.

    state is one of "", "pending", "success", "failure", "error".
    age_seconds is wall-clock seconds since the status was posted (0.0 if missing).
    """
    raw = _gh_api([f"repos/{repo}/commits/{sha}/status"])
    payload = json.loads(raw)
    matching = [s for s in payload.get("statuses", []) if s.get("context") == STATUS_CONTEXT]
    if not matching:
        return "", 0.0
    # GitHub returns statuses ordered most-recent first within the combined
    # endpoint, but be explicit: pick the one with the latest updated_at.
    latest = max(matching, key=lambda s: s.get("updated_at", ""))
    state = latest.get("state", "")
    updated_at = latest.get("updated_at", "")
    try:
        from datetime import datetime

        ts = datetime.strptime(updated_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        age = (datetime.now(UTC) - ts).total_seconds()
    except (ValueError, TypeError):
        age = 0.0
    return state, age


def _has_in_flight_run(repo: str, sha: str) -> bool:
    """True if there is a non-completed osdc-pr-validate run for `sha`.

    A completed-but-cancelled run (e.g. runner eviction) is NOT considered
    in-flight, which lets us re-dispatch when the post job never ran.
    """
    raw = _gh_api(
        [
            f"repos/{repo}/actions/workflows/{VALIDATE_WORKFLOW}/runs",
            "-X",
            "GET",
            "-f",
            f"head_sha={sha}",
            "-f",
            "per_page=20",
        ]
    )
    payload = json.loads(raw)
    for run in payload.get("workflow_runs", []):
        status = run.get("status", "")
        if status in ("queued", "in_progress", "waiting", "pending", "requested"):
            return True
    return False


def _dispatch_validate(repo: str, pr_number: str, head_sha: str) -> None:
    _gh_workflow_run(
        repo=repo,
        workflow=VALIDATE_WORKFLOW,
        ref=DISPATCH_REF,
        inputs={"pr_number": pr_number, "head_sha": head_sha},
    )
    print(f"Dispatched {VALIDATE_WORKFLOW} on {DISPATCH_REF} for PR #{pr_number} (sha {head_sha[:12]}).")


def main() -> int:
    repo = _env("REPO")
    pr_number = _env("PR_NUMBER")
    head_sha = _env("HEAD_SHA")

    poll_interval = _int_env("POLL_INTERVAL_SEC", 90)
    max_wait = _int_env("MAX_WAIT_SEC", 6 * 60 * 60)
    pending_grace = _int_env("PENDING_GRACE_SEC", 30 * 60)

    if not re.fullmatch(r"[0-9a-f]{40}", head_sha):
        _emit_output("close-validation-failed", f"HEAD_SHA is not a 40-char hex SHA: {head_sha!r}")
        return 0

    pr = _read_pr(repo, pr_number)
    head_ref = pr.get("head", {}).get("ref", "")
    if not HEAD_REF_PATTERN.fullmatch(head_ref):
        _emit_output(
            "close-branch-name-invalid",
            f"head.ref {head_ref!r} does not match renovate-runner/<safe-chars> pattern",
        )
        return 0

    deadline = time.monotonic() + max_wait
    dispatched = False
    dispatched_at: float | None = None

    while True:
        # Bail if the PR head moved while we were waiting. A force-push
        # invalidates the SHA we validated; only the new head's status
        # would be meaningful, and Renovate will reopen on the next run.
        try:
            current_pr = _read_pr(repo, pr_number)
        except RuntimeError as e:
            print(f"::warning::failed to re-read PR: {e}")
            current_pr = None
        if current_pr is not None:
            current_sha = current_pr.get("head", {}).get("sha", "")
            if current_sha and current_sha != head_sha:
                _emit_output(
                    "close-head-moved",
                    f"PR head moved from {head_sha} to {current_sha} during validation",
                )
                return 0

        try:
            state, age = _read_status(repo, head_sha)
        except RuntimeError as e:
            print(f"::warning::failed to read status: {e}")
            state, age = "", 0.0

        if state == "success":
            print(f"{STATUS_CONTEXT} is green for {head_sha} — proceeding.")
            _emit_output("approve", f"{STATUS_CONTEXT} success on {head_sha}")
            return 0

        if state in ("failure", "error"):
            print(f"::warning::{STATUS_CONTEXT} is {state} for {head_sha} — PR will be auto-closed.")
            _emit_output(
                "close-validation-failed",
                f"{STATUS_CONTEXT} status was {state} on {head_sha}",
            )
            return 0

        if state == "pending":
            # Pending is normal while the validate run is executing. But if it
            # has been pending past the grace period AND no run is currently
            # in flight for this SHA (post job died, runner evicted, etc.),
            # the status will never flip — re-dispatch.
            if age > pending_grace:
                try:
                    in_flight = _has_in_flight_run(repo, head_sha)
                except RuntimeError as e:
                    print(f"::warning::failed to check in-flight runs: {e}")
                    in_flight = True  # Be conservative — assume in flight, keep waiting.
                if not in_flight:
                    print(f"Status pending for {age:.0f}s with no in-flight run — re-dispatching.")
                    try:
                        _dispatch_validate(repo, pr_number, head_sha)
                        dispatched_at = time.monotonic()
                    except RuntimeError as e:
                        _emit_output("close-dispatch-failed", f"re-dispatch failed: {e}")
                        return 0
        elif state == "":
            if not dispatched:
                print(f"No {STATUS_CONTEXT} status on {head_sha} yet — dispatching.")
                try:
                    _dispatch_validate(repo, pr_number, head_sha)
                except RuntimeError as e:
                    _emit_output("close-dispatch-failed", f"initial dispatch failed: {e}")
                    return 0
                dispatched = True
                dispatched_at = time.monotonic()
            else:
                # We dispatched but no status posted yet. The pre job posts
                # pending almost immediately on a free runner; if we still
                # see nothing after pending_grace, something is wrong.
                if dispatched_at is not None and time.monotonic() - dispatched_at > pending_grace:
                    print(f"::warning::dispatched {pending_grace}s ago but no status posted — re-dispatching.")
                    try:
                        _dispatch_validate(repo, pr_number, head_sha)
                        dispatched_at = time.monotonic()
                    except RuntimeError as e:
                        _emit_output("close-dispatch-failed", f"re-dispatch failed: {e}")
                        return 0
        else:
            print(f"::warning::unexpected {STATUS_CONTEXT} state {state!r} on {head_sha} — continuing to poll.")

        if time.monotonic() >= deadline:
            _emit_output(
                "close-validation-timeout",
                f"timed out after {max_wait // 3600}h waiting for {STATUS_CONTEXT} on {head_sha}",
            )
            return 0

        time.sleep(poll_interval)


if __name__ == "__main__":
    sys.exit(main())
