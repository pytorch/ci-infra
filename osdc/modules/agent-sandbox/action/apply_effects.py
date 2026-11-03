#!/usr/bin/env python3
"""Apply the effects a sandbox run proposed and the dispatcher accepted. Stdlib only.

This is the only step that writes, and it runs outside the sandbox with the action's
`github-token` input (the calling job's GITHUB_TOKEN by default, which can write only to the
job's own repository). It reads the result the action's own /run step wrote moments earlier in the
same job — never an artifact from another job, which an untrusted job could have forged.
It still re-checks every effect: it must target this job's repository and pull request,
and a comment is posted as a pull-request review bound to the reviewed commit, a check run
is created on that commit, and neither is written if the pull request has moved on.
Each effect is recorded in the job log and the step summary.

For PR-triggered callers, run the WHOLE call (the /run step and this one) in a trusted
job — a workflow_run job on the default branch, fed only the PR number by the untrusted
stage — so the write token never sits beside untrusted code.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request

SHA_RE = re.compile(r"[0-9a-f]{40}")
TIMEOUT_S = 30
KINDS = {"pr_comment", "check_run"}


class ApplyError(RuntimeError):
    pass


def _escape(value) -> str:
    return str(value).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


class GitHub:
    def __init__(self, api: str, token: str, opener=None):
        self.api = api.rstrip("/")
        self.token = token
        self.opener = opener or urllib.request.build_opener()

    def call(self, method: str, path: str, body: dict | None = None) -> dict:
        request = urllib.request.Request(  # noqa: S310  (GITHUB_API_URL from the runner)
            f"{self.api}{path}",
            data=json.dumps(body).encode() if body is not None else None,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with self.opener.open(request, timeout=TIMEOUT_S) as response:
                return json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            raise ApplyError(f"{method} {path} -> HTTP {exc.code}") from None


def apply(effect: dict, github: GitHub, repository: str, pr_number: int) -> str:
    """Apply one effect, or raise ApplyError. Returns a one-line audit record."""
    kind = effect.get("effect")
    if kind not in KINDS:
        raise ApplyError(f"unknown effect {kind!r}")
    if effect.get("repo") != repository:
        raise ApplyError(f"effect targets {effect.get('repo')!r}, not {repository!r}")
    head = effect.get("head_sha")
    if not isinstance(head, str) or not SHA_RE.fullmatch(head):
        raise ApplyError("effect carries no commit")
    body = effect.get("body")
    if not isinstance(body, str) or not body.strip():
        raise ApplyError("effect has no body")

    pull = github.call("GET", f"/repos/{repository}/pulls/{pr_number}")
    current = (pull.get("head") or {}).get("sha")
    if current != head:
        return f"skipped {kind}: the pull request moved from {head[:12]} to {str(current)[:12]} since the review"

    if kind == "pr_comment":
        # A review, not an issue comment: `commit_id` binds it to the reviewed commit, so
        # a push that lands between the check above and this call leaves the comment
        # marked as on an outdated commit instead of passing it off as current.
        made = github.call(
            "POST",
            f"/repos/{repository}/pulls/{pr_number}/reviews",
            {"commit_id": head, "event": "COMMENT", "body": body},
        )
        return f"posted pr_comment {made.get('html_url', '')}"
    name, conclusion, title = effect.get("name"), effect.get("conclusion"), effect.get("title")
    if not all(isinstance(v, str) and v for v in (name, conclusion, title)):
        raise ApplyError("check_run needs name, conclusion and title")
    made = github.call(
        "POST",
        f"/repos/{repository}/check-runs",
        {
            "name": name,
            "head_sha": head,
            "status": "completed",
            "conclusion": conclusion,
            "output": {"title": title, "summary": body},
        },
    )
    return f"created check_run {name} ({conclusion}) {made.get('html_url', '')}"


def main(env: dict | None = None, opener=None) -> int:
    env = dict(os.environ) if env is None else env
    try:
        pr_number = int(env.get("INPUT_PR_NUMBER", ""))
        if pr_number <= 0:
            raise ValueError
    except ValueError:
        print("::error::input `pr-number` must be a positive integer to apply effects", flush=True)
        return 1
    repository = env.get("INPUT_REPOSITORY", "").strip()
    token = env.get("INPUT_GITHUB_TOKEN", "").strip()
    if not repository or not token:
        print("::error::inputs `repository` and `github-token` are required to apply effects", flush=True)
        return 1
    # The job's own token can write only to the repository the job runs in. A caller that
    # reviews another repository (ciforge-pr-review reads pytorch/pytorch from
    # pytorch/ciforge) must pass a token for the target; say so before touching the API.
    job_repository = env.get("GITHUB_REPOSITORY", "").strip()
    if env.get("INPUT_TOKEN_IS_JOB_TOKEN") == "true" and repository.lower() != job_repository.lower():
        print(
            f"::error::the job's GITHUB_TOKEN can write only to {_escape(job_repository)}; pass a "
            f"`github-token` that can write to {_escape(repository)}, such as a GitHub App installation token",
            flush=True,
        )
        return 1
    try:
        with open(env.get("INPUT_RESULT_FILE", ""), encoding="utf-8") as handle:
            result = json.load(handle)
    except (OSError, ValueError) as exc:
        print(f"::error::cannot read the run result: {_escape(exc)}", flush=True)
        return 1
    effects = result.get("effects") if isinstance(result, dict) else None
    if not effects:
        print("no effects to apply", flush=True)
        return 0

    github = GitHub(env.get("GITHUB_API_URL", "https://api.github.com"), token, opener)
    records, failed = [], False
    for effect in effects if isinstance(effects, list) else []:
        try:
            records.append(apply(effect if isinstance(effect, dict) else {}, github, repository, pr_number))
        except (ApplyError, OSError, ValueError) as exc:
            failed = True
            records.append(f"FAILED: {exc}")
    for record in records:
        print(_escape(record), flush=True)
    summary = env.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write("### agent-sandbox effects\n" + "".join(f"- {r}\n" for r in records))
    if failed:
        print("::error::one or more effects could not be applied", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
