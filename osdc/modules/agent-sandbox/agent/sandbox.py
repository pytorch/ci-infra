#!/usr/bin/env python3
"""Sandbox task library: check out a public repo, then ask Bedrock about it.

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
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

import agent_loop

REGION = os.environ.get("AWS_REGION", "us-east-1")
SIGV4_PROXY = os.environ.get("SIGV4_PROXY", "sigv4-proxy.ai-sandbox.svc.cluster.local:8080")
DEFAULT_MODEL = os.environ.get("BEDROCK_DEFAULT_MODEL_ID", "")
CLONE_TIMEOUT_S = 120
BEDROCK_TIMEOUT_S = 120
# The proxy's response is caller-influenced (the prompt is), so bound it.
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_ERROR_BODY_BYTES = 8 * 1024
READ_CHUNK_BYTES = 64 * 1024
# How much of a diff reaches the prompt. Beyond it the model is told the diff was cut.
# run_task cuts it further if the whole first prompt would not fit the context window.
MAX_DIFF_BYTES = 200 * 1024
# Bytes the changed-file list may take in the prompt.
MAX_PROMPT_FILE_BYTES = 32 * 1024

SHA_RE = re.compile(r"[0-9a-f]{40}")
# The result travels in the pod log, of which the dispatcher reads 1 MiB. Every
# unbounded field gets a share: each name list 256 KiB of JSON (see bounded_names), each
# error message 8 KiB. The report is bounded by max_tokens.
MAX_RESULT_FILE_BYTES = 256 * 1024
MAX_ERROR_CHARS = 8 * 1024


# Same rules as the dispatcher's http_api.valid_ref, re-checked here because the value
# reaches git as an argument in this pod.
def valid_ref(name: str) -> bool:
    """A branch or tag name git would accept (`git check-ref-format --branch` rules), or a
    full commit sha. It becomes a git argument, so it may not start with `-`."""
    if not isinstance(name, str) or not 0 < len(name) <= 1024:
        return False
    if SHA_RE.fullmatch(name):
        return True
    if name.startswith(("-", "/")) or name.endswith(("/", ".", ".lock")):
        return False
    if any(ord(c) < 0x20 or ord(c) == 0x7F or c in " ~^:?*[\\" for c in name):
        return False
    if ".." in name or "//" in name or "@{" in name or name == "@":
        return False
    return not any(part.startswith(".") or part.endswith(".lock") for part in name.split("/"))


def _git(args: list[str], cwd: str | None = None, timeout: int = 30) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        # Paths are bytes to git; a non-UTF-8 name must not fail the whole listing.
        errors="backslashreplace",
        timeout=timeout,
        # A private repo would otherwise make git prompt for a username and block
        # until the timeout instead of failing.
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    ).stdout


def clone_repo(repo: str, ref: str, dest: str) -> int:
    """Shallow, anonymous checkout of `ref` — a branch, tag or commit sha — of a public
    repo. Returns the tracked-file count.

    `init` + `fetch --depth 1` + `checkout FETCH_HEAD` rather than `clone --branch`,
    because only the former accepts a commit sha (GitHub serves any reachable commit by
    sha). A name is resolved against the remote's advertised refs by EXACT name —
    `refs/heads/<name>`, then `refs/tags/<name>` — and the resulting sha is fetched. A
    bare refspec would let git reinterpret it: `+main` is a forced fetch of `main`, and
    `refs/heads/v1` expands to a tag called `refs/heads/v1` when no such branch exists.
    No credential is involved: a private repo fails the fetch.
    """
    if not valid_ref(ref):
        raise ValueError(f"not a valid branch, tag or commit: {ref!r}")
    _git(["init", "-q", dest])
    _git(["remote", "add", "origin", f"https://github.com/{repo}.git"], cwd=dest)
    sha = ref if SHA_RE.fullmatch(ref) else resolve_ref(dest, ref)
    _git(["fetch", "-q", "--depth", "1", "origin", sha], cwd=dest, timeout=CLONE_TIMEOUT_S)
    _git(["checkout", "-q", "--detach", "FETCH_HEAD"], cwd=dest)
    return len([line for line in _git(["ls-files", "-z"], cwd=dest).split("\0") if line])


def resolve_ref(dest: str, ref: str) -> str:
    """The commit a branch or tag name points at on the remote, matched by exact name."""
    # A full ref name first when given one, then the branch and the tag of that name:
    # `refs/release` may itself be a branch called `refs/release`.
    names = ([ref] if ref.startswith("refs/") else []) + [f"refs/heads/{ref}", f"refs/tags/{ref}"]
    advertised: dict[str, str] = {}
    listing = _git(["ls-remote", "origin", *names, *(f"{n}^{{}}" for n in names)], cwd=dest, timeout=CLONE_TIMEOUT_S)
    # split("\n"), not splitlines(): a ref name may legally contain U+2028 and friends,
    # which splitlines() would treat as line breaks and so forge a second record.
    for line in listing.split("\n"):
        sha, _, name = line.partition("\t")
        advertised[name] = sha
    for name in names:
        # An annotated tag advertises the tag object and, as `name^{}`, the commit.
        sha = advertised.get(f"{name}^{{}}") or advertised.get(name)
        if sha:
            return sha
    raise ValueError(f"no branch or tag named {ref!r} in the repository")


def head_sha(dest: str) -> str:
    return _git(["rev-parse", "HEAD"], cwd=dest).strip()


def _git_to_file(args: list[str], cwd: str, path: str, timeout: int = 120) -> None:
    """Run git with stdout going to a file. Memory stays flat however large the output,
    the timeout and exit status are enforced by subprocess.run, and stderr is kept for
    the error message."""
    with open(path, "wb") as out:
        subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=True,
            stdout=out,
            stderr=subprocess.PIPE,
            timeout=timeout,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )


def diff_against(dest: str, base: str) -> tuple[list[str], int, str, bool]:
    """(changed files, total changed, diff text, truncated) from commit `base` to HEAD.

    Two shallow commits are enough: `git diff A B` compares trees and needs no history.
    The caller passes the merge base for a PR-shaped diff. Output goes to files outside
    the checkout and only a bounded prefix is read back, because the diff goes into the
    prompt. The trailing `--` keeps a tracked file named like a revision from making the
    arguments ambiguous. Raises on any git failure: a review of an empty diff it could
    not compute would look like a clean review.
    """
    if not isinstance(base, str) or not SHA_RE.fullmatch(base):
        raise ValueError(f"base must be a full commit sha, got {base!r}")
    _git(["fetch", "-q", "--depth", "1", "origin", base], cwd=dest, timeout=CLONE_TIMEOUT_S)
    with tempfile.TemporaryDirectory() as scratch:
        names_path, patch_path = os.path.join(scratch, "names"), os.path.join(scratch, "patch")
        _git_to_file(["diff", "--name-only", "-z", base, "HEAD", "--"], dest, names_path)
        _git_to_file(["diff", "--no-color", "--no-ext-diff", base, "HEAD", "--"], dest, patch_path)
        with open(names_path, "rb") as handle:
            files = [f.decode(errors="backslashreplace") for f in handle.read().split(b"\0") if f]
        with open(patch_path, "rb") as handle:
            patch = handle.read(MAX_DIFF_BYTES + 1)
    # backslashreplace, not ignore: a non-UTF-8 byte must stay visible, or `café` changed
    # to `cafè` in a Latin-1 file reads as no change at all. Escaping can quadruple a
    # byte, so the cap is applied again to the text that actually reaches the prompt.
    text = patch[:MAX_DIFF_BYTES].decode(errors="backslashreplace")
    capped = text.encode()[:MAX_DIFF_BYTES].decode(errors="ignore")
    truncated = len(patch) > MAX_DIFF_BYTES or len(capped) < len(text)
    return files, len(files), capped, truncated


def bounded_names(files: list[str], budget: int = MAX_RESULT_FILE_BYTES) -> list[str]:
    """As many leading names as fit in `budget` bytes of result JSON. The dispatcher reads
    at most 1 MiB of the pod log, and a result cut short is no result at all."""
    kept, used = [], 2
    for name in files:
        used += len(json.dumps(name)) + 2
        if used > budget:
            break
        kept.append(name)
    return kept


def top_level_entries(dest: str) -> list[str]:
    """Top-level tracked entries (`dir/` for trees). Grounding for the prompt: with
    only a file *count* the model invents a plausible listing, so the report says
    nothing about the repo the agent actually cloned."""
    # Raw bytes, and the entry type from git itself: text mode would turn a CR in a name
    # into LF, and an escaped non-UTF-8 name is not a path isdir() can check.
    raw = subprocess.run(
        ["git", "ls-tree", "-z", "HEAD"],
        cwd=dest,
        check=True,
        capture_output=True,
        timeout=30,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    ).stdout
    entries = []
    for record in raw.split(b"\0"):
        if not record:
            continue
        meta, _, name = record.partition(b"\t")
        display = name.decode(errors="backslashreplace")
        entries.append(f"{display}/" if meta.split(b" ")[1:2] == [b"tree"] else display)
    return entries


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


class BedrockHTTPError(RuntimeError):
    """An HTTP error from Bedrock, already summarised (bedrock_error_summary).

    Summarised inside the call, not by the caller: reading the error body can block, and
    the agent loop bounds a model call's wall-clock time only while it is running."""


def invoke_model(model: str, fields: dict, timeout: float = BEDROCK_TIMEOUT_S) -> dict:
    """One Bedrock InvokeModel (Messages API) call through the sigv4 proxy — unsigned in,
    signed out — returning the parsed response."""
    body = json.dumps({"anthropic_version": "bedrock-2023-05-31", "max_tokens": 1024, **fields}).encode()
    # The model id is one path segment and has to be encoded as one: an inference
    # profile or foundation model ARN is a documented identifier and contains "/",
    # which would otherwise split the path so the request no longer names an invoke.
    # It also stops a model id from steering the path the proxy signs — the proxy runs
    # with no --name and forwards whatever path it is handed, leaving only its IRSA
    # policy behind this.
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
    timeout = min(timeout, BEDROCK_TIMEOUT_S)
    deadline = time.monotonic() + timeout
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            payload = json.loads(_read_bounded(resp, MAX_RESPONSE_BYTES, deadline))
    except urllib.error.HTTPError as exc:
        raise BedrockHTTPError(bedrock_error_summary(exc)) from None
    if not isinstance(payload, dict):
        raise ValueError("bedrock returned a non-object response")
    return payload


def invoke_bedrock(model: str, prompt: str) -> str:
    """One prompt in, the first text block out."""
    content = (
        invoke_model(model, {"messages": [{"role": "user", "content": [{"type": "text", "text": prompt}]}]}).get(
            "content"
        )
        or []
    )
    return content[0]["text"] if content else ""


def build_prompt(
    repo: str, ref: str, task: str, file_count: int, entries: list[str], change: dict | None = None
) -> str:
    """Prompt the model with what the agent actually observed in the clone, and
    tell it not to fill gaps — an ungrounded answer looks identical to a correct
    one, which would make the canary's 'Bedrock returned a report' assertion
    meaningless."""
    lines = [
        f"You are inspecting a checkout of {repo} at ref {ref}.",
        f"It has {file_count} tracked files.",
    ]
    if entries:
        shown = bounded_names(entries, MAX_PROMPT_FILE_BYTES)
        complete = "complete list" if len(shown) == len(entries) else f"first {len(shown)} of {len(entries)}"
        lines += ["", f"Top-level entries ({complete}, `/` marks a directory):", *(f"  {e}" for e in shown)]
    if change:
        files = change["files"]
        lines += ["", f"The change under review, against base {change['base']}: {len(files)} file(s) changed."]
        shown = bounded_names(files, MAX_PROMPT_FILE_BYTES)
        lines += [f"  {f}" for f in shown]
        if len(files) > len(shown):
            lines.append(f"  ... and {len(files) - len(shown)} more")
        lines += [
            "",
            "Diff:" + (" (TRUNCATED — only the beginning is shown)" if change["truncated"] else ""),
            change["patch"],
        ]
    lines += [
        "",
        f"Task: {task}",
        "",
        "Use the list_dir, read_file and search tools to read what you need. Answer only "
        "from what is shown above and what the tools return. If that is not enough, say so "
        "instead of guessing — do not invent paths or file contents.",
    ]
    return "\n".join(lines)


def _error_text(exc: Exception) -> str:
    """A stage failure as text for `errors`: git's stderr when there is some (it may be
    bytes), else the exception. Bytes here would make the result unserializable."""
    stderr = getattr(exc, "stderr", None)
    if isinstance(stderr, bytes):
        stderr = stderr.decode(errors="replace")
    return (stderr or str(exc))[:MAX_ERROR_CHARS]


def _effects_field(spec: dict) -> list:
    """The writes the manifest allows, from SANDBOX_EFFECTS (JSON). Malformed means none."""
    try:
        effects = json.loads(spec.get("effects") or "[]")
    except (TypeError, ValueError):
        return []
    return effects if isinstance(effects, list) else []


def _str_field(spec: dict, key: str, default: str) -> str:
    """A non-empty string field, or `default` for anything else.

    `spec.get("ref", "main")` hands back None for an explicit `{"ref": null}`, and
    None goes on to git as a command argument; `spec.get("ref") or "main"` lets 0 or
    [] through the same way. /run rejects wrong types at the boundary — this keeps
    run_task's "never raises" contract for direct callers (canary, tests) as well.
    """
    value = spec.get(key)
    return value if isinstance(value, str) and value else default


# Left between the agent loop's end and the task's hard deadline, for printing the result
# and for clock skew between the dispatcher's node and this one.
RESULT_MARGIN_S = 30


def loop_time_limit(deadline, now: float) -> float:
    """Seconds the agent loop may run: LOOP_DEADLINE_S, or less when the task's own
    deadline (epoch seconds from the dispatcher, SANDBOX_DEADLINE) is nearer. Scheduling,
    the fetch and the diff have already spent part of it."""
    try:
        at = float(deadline)
    except (TypeError, ValueError):
        return agent_loop.LOOP_DEADLINE_S
    if at != at or at in (float("inf"), float("-inf")):  # NaN or infinite: ignore it
        return agent_loop.LOOP_DEADLINE_S
    return max(0.0, min(agent_loop.LOOP_DEADLINE_S, at - now - RESULT_MARGIN_S))


def run_task(spec: dict) -> dict:
    """Clone the repo, then (optionally) ask Bedrock about it. Never raises —
    each stage's failure is captured so callers see exactly what worked."""
    repo = spec["repo"]
    ref = _str_field(spec, "ref", "main")
    task = _str_field(spec, "task", "Summarize this repository.")
    model = _str_field(spec, "model", DEFAULT_MODEL)
    base = _str_field(spec, "base", "")
    allowed_effects = _effects_field(spec)
    result: dict = {"cloned": False, "file_count": 0, "top_level": [], "report": "", "errors": {}}

    with tempfile.TemporaryDirectory() as workdir:
        try:
            result["file_count"] = clone_repo(repo, ref, workdir)
            result["cloned"] = True
            result["head_sha"] = head_sha(workdir)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError) as exc:
            result["errors"]["clone"] = _error_text(exc)
            return result

        change = None
        if base:
            try:
                files, total, patch, truncated = diff_against(workdir, base)
                change = {"base": base, "files": files, "patch": patch, "truncated": truncated}
                result["changed_files"] = bounded_names(files)
                result["changed_files_total"] = total
                result["diff_truncated"] = truncated
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError) as exc:
                # A diff we cannot compute is reported, and the task stops: answering a
                # review request without the change would look like a review.
                result["errors"]["diff"] = _error_text(exc)
                return result

        if not model:
            result["errors"]["bedrock"] = "no model configured (set BEDROCK_DEFAULT_MODEL_ID or pass 'model')"
            return result

        try:
            entries = top_level_entries(workdir)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            # Grounding is best-effort — a repo we can't list is still worth asking about.
            result["errors"]["listing"] = _error_text(exc)
            entries = []
        result["top_level"] = bounded_names(entries)
        result["top_level_total"] = len(entries)

        prompt = build_prompt(repo, ref, task, result["file_count"], entries, change)
        # The first request has no token count to go on, so it must fit the window at one
        # token per byte. The diff is the part that can give way; the loop refuses a
        # prompt still too large after that.
        budget = agent_loop.prompt_budget_bytes()
        if change and change["patch"] and len(prompt.encode()) > budget:
            # Marked first: the TRUNCATED note itself takes a few bytes of the budget.
            change["truncated"] = True
            result["diff_truncated"] = True
            prompt = build_prompt(repo, ref, task, result["file_count"], entries, change)
            excess = len(prompt.encode()) - budget
            if excess > 0:
                patch = change["patch"].encode()
                change["patch"] = patch[: max(0, len(patch) - excess)].decode(errors="ignore")
                prompt = build_prompt(repo, ref, task, result["file_count"], entries, change)
        try:
            tools = agent_loop.RepoTools(workdir, allowed_effects)
            limit = loop_time_limit(spec.get("deadline"), time.time())
            outcome = agent_loop.run_agent(invoke_model, model, prompt, tools, time_limit_s=limit)
            if tools.proposals and "error" not in outcome:
                result["effects"] = tools.proposals
            elif tools.proposals:
                # A run that did not finish cleanly writes nothing: its proposals may have
                # been made before it read what would have changed its mind.
                result["errors"]["effects"] = "proposals dropped: the agent did not finish"
            result["report"] = outcome["report"]
            result["turns"] = outcome["turns"]
            result["tool_calls"] = outcome["tool_calls"]
            if "error" in outcome:
                result["errors"]["agent"] = outcome["error"]
        except BedrockHTTPError as exc:
            result["errors"]["bedrock"] = str(exc)
        except urllib.error.HTTPError as exc:
            result["errors"]["bedrock"] = bedrock_error_summary(exc)
        except (OSError, http.client.HTTPException, KeyError, TypeError, ValueError, RecursionError) as exc:
            # OSError covers URLError and TimeoutError; HTTPException covers the
            # truncated body (IncompleteRead) and the reset status line
            # (RemoteDisconnected) that a proxy restart produces mid-response.
            # Anything escaping here closes the connection on the caller, which
            # cannot be told apart from the pod being gone — the single answer this
            # endpoint exists to avoid giving. RecursionError: a deeply nested response
            # overflows json.loads.
            result["errors"]["bedrock"] = str(exc)

    return result
