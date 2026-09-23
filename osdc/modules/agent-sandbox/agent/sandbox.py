#!/usr/bin/env python3
"""Sandbox task library: clone a public repo, then ask Bedrock about it.

Imported by task.py, which runs it once per pod. It holds NO credentials — it clones
public repos anonymously and reaches Bedrock through the sigv4 proxy, which signs
with its own IRSA identity. This process never sees a token.

The HTTP surface lives in the dispatcher (../dispatcher/http_api.py): it turns each
request into one Job, so nothing here has to serialize or refuse concurrent work.
"""

from __future__ import annotations

import http.client
import json
import os
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

REGION = os.environ.get("AWS_REGION", "us-east-1")
SIGV4_PROXY = os.environ.get("SIGV4_PROXY", "sigv4-proxy.ai-sandbox.svc.cluster.local:8080")
# Set to reach PRIVATE repositories: the proxy holds the GitHub credential this process
# deliberately does not. Empty means fetch github.com directly and anonymously, which is
# the pre-proxy behaviour and reaches public repositories only.
GIT_PROXY = os.environ.get("GIT_PROXY", "")
DEFAULT_MODEL = os.environ.get("BEDROCK_DEFAULT_MODEL_ID", "")
CLONE_TIMEOUT_S = 120
BEDROCK_TIMEOUT_S = 120
# The fetch carries the whole tree, so it gets CLONE_TIMEOUT_S; init, checkout and
# ls-files are local and near-instant, and a separate short budget keeps a wedged one
# from spending the fetch's.
GIT_STEP_TIMEOUT_S = 30
# The proxy's response is caller-influenced (the prompt is), so bound it.
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_ERROR_BODY_BYTES = 8 * 1024
READ_CHUNK_BYTES = 64 * 1024


def clone_repo(repo: str, ref: str, dest: str) -> int:
    """Shallow, anonymous checkout of one ref of a public repo. Returns the tracked-file
    count.

    fetch-then-checkout rather than `git clone --branch`, because `--branch` resolves a
    BRANCH OR TAG and nothing else — which left the two refs a review needs unreachable:
    `refs/pull/<n>/head` and a commit sha. A fetch takes any of the four, and a pull
    request's head ref lives in the BASE repository, so this reaches a fork's PR without
    ever naming the fork.

    A PRIVATE repository needs a credential, which this process never holds, so those
    fetches go through GIT_PROXY — see kubernetes/base/git-proxy.yaml. Unset, this talks
    to github.com anonymously and reaches public repositories only.

    Fetching a bare sha needs the server to allow it (`uploadpack.allowReachableSHA1InWant`).
    github.com does: verified 2026-09-23 by fetching a full commit sha of pytorch/ci-infra
    into an empty repo. It is a server-side setting, so a future GitHub Enterprise or
    mirror host may refuse — the failure is git's own "want ... not valid", captured as a
    clone error like any other.
    """
    url = f"http://{GIT_PROXY}/{repo}.git" if GIT_PROXY else f"https://github.com/{repo}.git"
    # Plain HTTP to the proxy is deliberate and matches the Bedrock path: it is a
    # ClusterIP inside the namespace, the NetworkPolicy admits only task pods, and the
    # request carries no credential to protect — the proxy adds one on its way out.
    #
    # GIT_TERMINAL_PROMPT=0 still matters with a proxy in front: a repo the proxy's
    # allowlist refuses answers 403, and git would otherwise prompt for a username and
    # block until the timeout instead of failing with the reason.
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}

    def git(*args: str, timeout: int) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *args], check=True, capture_output=True, text=True, timeout=timeout, env=env)

    git("init", "-q", dest, timeout=GIT_STEP_TIMEOUT_S)
    # The URL is passed to fetch rather than configured as a remote: nothing here pushes
    # or re-fetches, and an unconfigured remote is one less thing a later step can follow.
    # `--` is load-bearing. git permutes arguments, so without it a `ref` of
    # `--upload-pack=...` is parsed as an OPTION rather than a refspec: over https that
    # silently fetches the default branch (wrong tree, empty `errors`), and over a
    # local/ssh transport git executes it. The old `--branch <ref>` form was immune
    # because it consumed ref as an option VALUE; this restores that property.
    git("-C", dest, "fetch", "--depth", "1", "--", url, ref, timeout=CLONE_TIMEOUT_S)
    # Detached at FETCH_HEAD. There is no local branch and no `origin`, which is correct
    # for a tree that is read once and thrown away with the pod.
    # CLONE_TIMEOUT_S, not the short budget: this is the write half of what `git clone`
    # used to do in one call, and writing a large tree under gVisor is slow enough that
    # 30s would fail a fetch that fully succeeded.
    git("-C", dest, "checkout", "-q", "FETCH_HEAD", timeout=CLONE_TIMEOUT_S)

    listing = git("-C", dest, "ls-files", timeout=GIT_STEP_TIMEOUT_S)
    return len([line for line in listing.stdout.splitlines() if line.strip()])


def top_level_entries(dest: str) -> list[str]:
    """Top-level tracked entries (`dir/` for trees). Grounding for the prompt: with
    only a file *count* the model invents a plausible listing, so the report says
    nothing about the repo the agent actually cloned."""
    listing = subprocess.run(
        ["git", "-C", dest, "ls-tree", "--name-only", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    names = [line.strip() for line in listing.stdout.splitlines() if line.strip()]
    return [f"{n}/" if os.path.isdir(os.path.join(dest, n)) else n for n in names]


def _read_bounded(resp, limit: int, deadline: float) -> bytes:
    """Read at most `limit` bytes, giving up at `deadline` (a monotonic timestamp).

    BEDROCK_TIMEOUT_S is urllib's per-operation socket timeout, not a wall clock: a
    proxy that trickles one byte at a time resets it on every chunk and would hold
    the single task slot — and grow this pod's memory — for as long as it likes.
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        if time.monotonic() > deadline:
            raise TimeoutError(f"bedrock response incomplete after {BEDROCK_TIMEOUT_S}s")
        chunk = resp.read(READ_CHUNK_BYTES)
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > limit:
            raise ValueError(f"bedrock response exceeded {limit} bytes")
        chunks.append(chunk)


def bedrock_error_summary(exc: urllib.error.HTTPError) -> str:
    """Status line plus the AWS error code, which `str(HTTPError)` omits.

    'HTTP Error 403: Forbidden' cannot tell a model that isn't enabled for the
    account from a throttle, and those are the two likeliest failures of the
    credential path this endpoint exists to demonstrate. The code only, never the
    message: an AccessDenied message names the role ARN the proxy signs with, and
    any caller the NetworkPolicy allows can read this response back.
    """
    code = exc.headers.get("x-amzn-errortype", "") if exc.headers else ""
    if not code:
        try:
            payload = json.loads(exc.read(MAX_ERROR_BODY_BYTES) or "{}")
            if isinstance(payload, dict):
                code = payload.get("__type") or payload.get("code") or ""
        except (OSError, http.client.HTTPException, ValueError):
            code = ""
    # Both forms carry a suffix or prefix to drop: a __type reads
    # com.amazon.coral.service<hash>AccessDeniedException, a header reads
    # ThrottlingException followed by a colon and a URL.
    code = str(code).split("#")[-1].split(":")[0].strip()
    return f"{exc} ({code})" if code else str(exc)


def invoke_bedrock(model: str, prompt: str) -> str:
    """Call Bedrock InvokeModel through the sigv4 proxy (unsigned in, signed out)."""
    body = json.dumps(
        {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": [{"type": "text", "text": prompt}]}],
        }
    ).encode()
    # The model id is one path segment and has to be encoded as one: an inference
    # profile or foundation model ARN is a documented identifier and contains "/",
    # which would otherwise split the path so the request no longer names an invoke.
    # It also stops a caller-supplied id (the /run body sets it) from steering the
    # path the proxy signs — the proxy runs with no --name and forwards whatever path
    # it is handed, leaving only its IRSA policy behind this.
    #
    # ":" is left alone deliberately, though botocore would encode it: it is a legal
    # path character, and every model id in use here ends in "…-v1:0", so encoding it
    # would change the one request shape known to work through this proxy today.
    req = urllib.request.Request(
        f"http://{SIGV4_PROXY}/model/{urllib.parse.quote(model, safe=':')}/invoke",
        data=body,
        method="POST",
        headers={
            "Host": f"bedrock-runtime.{REGION}.amazonaws.com",
            "Content-Type": "application/json",
        },
    )
    deadline = time.monotonic() + BEDROCK_TIMEOUT_S
    with urllib.request.urlopen(req, timeout=BEDROCK_TIMEOUT_S) as resp:  # noqa: S310
        payload = json.loads(_read_bounded(resp, MAX_RESPONSE_BYTES, deadline))
    content = payload.get("content") or []
    return content[0]["text"] if content else ""


def build_prompt(repo: str, ref: str, task: str, file_count: int, entries: list[str]) -> str:
    """Prompt the model with what the agent actually observed in the clone, and
    tell it not to fill gaps — an ungrounded answer looks identical to a correct
    one, which would make the canary's 'Bedrock returned a report' assertion
    meaningless."""
    lines = [
        f"You are inspecting a checkout of {repo} at ref {ref}.",
        f"It has {file_count} tracked files.",
    ]
    if entries:
        lines += ["", "Top-level entries (complete list, `/` marks a directory):", *(f"  {e}" for e in entries)]
    lines += [
        "",
        f"Task: {task}",
        "",
        "Answer only from the listing above. If it doesn't contain the answer, say so "
        "instead of guessing — do not invent paths.",
    ]
    return "\n".join(lines)


def _str_field(spec: dict, key: str, default: str) -> str:
    """A non-empty string field, or `default` for anything else.

    `spec.get("ref", "main")` hands back None for an explicit `{"ref": null}`, and
    None goes on to git as a command argument; `spec.get("ref") or "main"` lets 0 or
    [] through the same way. /run rejects wrong types at the boundary — this keeps
    run_task's "never raises" contract for direct callers (canary, tests) as well.
    """
    value = spec.get(key)
    return value if isinstance(value, str) and value else default


def _pr_field(spec: dict) -> int:
    """The pull request number, or 0 for "not a review".

    Arrives as a STRING from task.py, which reads env vars, and as an int from a direct
    caller (tests, canary). Anything else is 0 rather than an exception: run_task never
    raises, and a spec the dispatcher already type-checked cannot get here malformed.
    """
    value = spec.get("pr")
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(value, 0)
    # isdecimal(), not isdigit(): '\u00b2'.isdigit() is True and int() then raises,
    # which would escape run_task entirely and leave task.py printing no JSON at all.
    if isinstance(value, str) and value.isdecimal():
        return int(value)
    return 0


def run_task(spec: dict) -> dict:
    """Check out a ref (or a pull request head) and ask Bedrock about it. Never raises —
    each stage's failure is captured so callers see exactly what worked."""
    repo = spec["repo"]
    ref = _str_field(spec, "ref", "main")
    task = _str_field(spec, "task", "Summarize this repository.")
    model = _str_field(spec, "model", DEFAULT_MODEL)
    pr = _pr_field(spec)
    result: dict = {"cloned": False, "file_count": 0, "top_level": [], "report": "", "errors": {}}

    if pr:
        # A pull request is just another ref to check out, and it wins over `ref`: the two
        # name different commits, and silently reviewing the branch a caller also sent
        # would be the wrong tree with no sign that anything was ignored.
        ref = f"refs/pull/{pr}/head"
        result["pr"] = pr

    with tempfile.TemporaryDirectory() as workdir:
        try:
            result["file_count"] = clone_repo(repo, ref, workdir)
            result["cloned"] = True
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            result["errors"]["clone"] = getattr(exc, "stderr", None) or str(exc)
            return result

        if not model:
            result["errors"]["bedrock"] = "no model configured (set BEDROCK_DEFAULT_MODEL_ID or pass 'model')"
            return result

        try:
            entries = top_level_entries(workdir)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            # Grounding is best-effort — a repo we can't list is still worth asking about.
            result["errors"]["listing"] = getattr(exc, "stderr", None) or str(exc)
            entries = []
        result["top_level"] = entries

        prompt = build_prompt(repo, ref, task, result["file_count"], entries)
        try:
            result["report"] = invoke_bedrock(model, prompt)
        except urllib.error.HTTPError as exc:
            result["errors"]["bedrock"] = bedrock_error_summary(exc)
        except (OSError, http.client.HTTPException, KeyError, TypeError, ValueError) as exc:
            # OSError covers URLError and TimeoutError; HTTPException covers the
            # truncated body (IncompleteRead) and the reset status line
            # (RemoteDisconnected) that a proxy restart produces mid-response.
            # Anything escaping here closes the connection on the caller, which
            # cannot be told apart from the pod being gone — the single answer this
            # endpoint exists to avoid giving.
            result["errors"]["bedrock"] = str(exc)

    return result
