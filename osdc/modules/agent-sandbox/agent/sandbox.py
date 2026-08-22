#!/usr/bin/env python3
"""Sandbox task library: clone a public repo, then ask Bedrock about it.

Imported by task.py, which runs it once per pod. It holds NO credentials — it clones
public repos anonymously and reaches Bedrock through the sigv4 proxy, which signs
with its own IRSA identity. This process never sees a token.

The HTTP surface lives in the dispatcher (../dispatcher/dispatcher.py): it turns each
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
DEFAULT_MODEL = os.environ.get("BEDROCK_DEFAULT_MODEL_ID", "")
CLONE_TIMEOUT_S = 120
BEDROCK_TIMEOUT_S = 120
GPU_QUERY_TIMEOUT_S = 30
# The proxy's response is caller-influenced (the prompt is), so bound it.
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_ERROR_BODY_BYTES = 8 * 1024
READ_CHUNK_BYTES = 64 * 1024


def clone_repo(repo: str, ref: str, dest: str) -> int:
    """Shallow, anonymous clone of a public repo. Returns the tracked-file count.

    FIXME(prototype): `ref` is a branch or tag only, never a commit sha —
    `git clone --branch` resolves nothing else, and a caller pinning a sha gets a
    clone failure that doesn't say why. Accepting a sha means `git init` +
    `fetch --depth 1 origin <ref>` + `checkout FETCH_HEAD`, and fetch-by-object-id
    is a server-side setting that has to be confirmed per repo. Deferred with the
    wider question of how the sandbox should check code out at all: a private repo
    needs a token, which this worker deliberately never holds, so that path wants
    mitmproxy in front of it the way Bedrock has the sigv4 proxy.
    """
    subprocess.run(
        ["git", "clone", "--depth", "1", "--branch", ref, f"https://github.com/{repo}.git", dest],
        check=True,
        capture_output=True,
        text=True,
        timeout=CLONE_TIMEOUT_S,
        # A private repo would otherwise make git prompt for a username and block
        # until the clone timeout instead of failing.
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    listing = subprocess.run(
        ["git", "-C", dest, "ls-files"],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
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


def gpu_info() -> dict:
    """What the sandbox can see of the GPU, or an error saying why it cannot see one.

    This is the end-to-end check for nvproxy: reaching nvidia-smi from inside a gVisor
    sandbox means the device nodes were injected, the ioctls were forwarded and the host
    driver answered. A GPU task whose result carries no `gpu` block ran without one.
    """
    try:
        proc = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,driver_version,memory.total", "--format=csv,noheader"],
            check=True,
            capture_output=True,
            text=True,
            timeout=GPU_QUERY_TIMEOUT_S,
        )
    except FileNotFoundError:
        # No nvidia-smi means the container hooks staged nothing — the usual symptom of a
        # GPU pod that landed without device injection.
        return {"error": "nvidia-smi not found in the sandbox"}
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        return {"error": (getattr(exc, "stderr", None) or str(exc)).strip()}

    devices = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    return {"devices": devices, "count": len(devices)}


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


def run_task(spec: dict) -> dict:
    """Clone the repo, then (optionally) ask Bedrock about it. Never raises —
    each stage's failure is captured so callers see exactly what worked."""
    repo = spec["repo"]
    ref = _str_field(spec, "ref", "main")
    task = _str_field(spec, "task", "Summarize this repository.")
    model = _str_field(spec, "model", DEFAULT_MODEL)
    result: dict = {"cloned": False, "file_count": 0, "top_level": [], "report": "", "errors": {}}

    # Only when the Job asked for a GPU: on a CPU task nvidia-smi is legitimately absent,
    # and reporting that as an error would make every ordinary result look broken.
    if os.environ.get("SANDBOX_GPU"):
        result["gpu"] = gpu_info()

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
