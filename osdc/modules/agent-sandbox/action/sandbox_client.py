#!/usr/bin/env python3
"""GitHub Actions client for the agent-sandbox dispatcher. Stdlib only.

Mints a GitHub OIDC token for the dispatcher's audience, POSTs /run under a named
capability manifest, and writes the result to the step outputs. That is the whole job.

Not a trust boundary: it holds no allowlist and makes no authorization decision. Any code
in an authorized job can mint the same token and call /run with curl, so the dispatcher
has to be correct against a hand-rolled request, and is.

Retry policy: only a 429 is retried, with jittered exponential backoff under one total
time budget. A 429 is refused before a task is admitted, so repeating it cannot run a
task twice. Anything after admission (a timeout, a dropped connection) is NOT retried:
the task may still be running and there is no idempotency key.
"""

from __future__ import annotations

import json
import math
import os
import random
import re
import secrets
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_ENDPOINT = "http://sandbox-agent.ai-sandbox.svc.cluster.local:8080"
AUDIENCE = "agent-service"
# A waiting /run holds the connection for the whole task, including a cold node. Longer
# than the dispatcher's own task deadline (900s) plus its polling grace and cleanup, so
# the structured "did not finish" answer arrives rather than a client-side timeout.
RUN_TIMEOUT_S = 1080
TOKEN_TIMEOUT_S = 30
HEALTHZ_TIMEOUT_S = 10
MAX_RESPONSE_BYTES = 4 * 1024 * 1024


TASK_ID_RE = re.compile(r"[0-9a-f]{12}")  # always fullmatch: `$` also accepts a trailing newline


class ClientError(RuntimeError):
    """A failure the step should report and exit non-zero on."""


def _command_data(value) -> str:
    """Escape text for a workflow-command line (GitHub's own escaping for `%`, CR, LF).

    Everything the dispatcher returns is partly task-controlled, and an unescaped newline
    would let it start its own `::add-mask::` or `::stop-commands::` line.
    """
    return str(value).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def mint_token(opener, env: dict, audience: str = AUDIENCE) -> str:
    """A GitHub Actions OIDC token for `audience`. Needs `permissions: id-token: write`."""
    url = env.get("ACTIONS_ID_TOKEN_REQUEST_URL", "").strip()
    bearer = env.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "").strip()
    if not url or not bearer:
        raise ClientError("no OIDC token available: the calling job needs `permissions: id-token: write`")
    sep = "&" if "?" in url else "?"
    request = urllib.request.Request(  # noqa: S310  (URL comes from the Actions runtime)
        f"{url}{sep}audience={urllib.parse.quote(audience)}", headers={"Authorization": f"bearer {bearer}"}
    )
    try:
        with opener.open(request, timeout=TOKEN_TIMEOUT_S) as response:
            document = json.loads(response.read(MAX_RESPONSE_BYTES))
    except (OSError, ValueError) as exc:
        raise ClientError(f"could not mint an OIDC token: {exc}") from None
    token = document.get("value") if isinstance(document, dict) else None
    if not isinstance(token, str) or not token:
        raise ClientError("the OIDC token request returned no token")
    # Masked before anything else can print it.
    print(f"::add-mask::{token}", flush=True)
    return token


def build_body(env: dict) -> dict:
    """The /run request from the action inputs."""
    manifest = env.get("INPUT_MANIFEST", "").strip()
    task = env.get("INPUT_TASK", "")
    if not manifest:
        raise ClientError("input `manifest` is required")
    if not task.strip():
        raise ClientError("input `task` is required")
    wait = env.get("INPUT_WAIT", "true").strip().lower()
    if wait not in ("true", "false"):
        raise ClientError(f"input `wait` must be true or false, got {wait!r}")
    body = {"manifest": manifest, "task": task, "wait": wait == "true"}
    for key in ("repo", "ref", "base"):
        value = env.get(f"INPUT_{key.upper()}", "").strip()
        if value:
            body[key] = value
    return body


def healthz(opener, endpoint: str) -> str:
    """One /healthz reading, for the failure message. Context, not a diagnosis: with two
    replicas it may describe the other pod."""
    try:
        with opener.open(f"{endpoint}/healthz", timeout=HEALTHZ_TIMEOUT_S) as response:
            return response.read(4096).decode(errors="replace")
    except (OSError, urllib.error.URLError) as exc:
        return f"unavailable ({exc})"


def call_run(
    opener,
    token_opener,
    env: dict,
    endpoint: str,
    body: dict,
    max_wait_s: float,
    sleep=time.sleep,
    clock=time.monotonic,
) -> tuple[int, dict]:
    """POST /run, retrying only 429 until `max_wait_s` has passed. Returns (status, json)."""
    deadline = clock() + max_wait_s
    delay = 5.0
    payload: dict = {}
    retrying = False
    while True:
        token = mint_token(token_opener, env)  # re-minted per attempt: tokens are short-lived
        # A retry must never be SENT past the budget, and minting can itself take time.
        if retrying and clock() >= deadline:
            return 429, payload
        request = urllib.request.Request(  # noqa: S310  (endpoint is an action input)
            f"{endpoint}/run",
            data=json.dumps(body).encode(),
            method="POST",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        )
        try:
            with opener.open(request, timeout=RUN_TIMEOUT_S) as response:
                status, raw = response.status, response.read(MAX_RESPONSE_BYTES)
        except urllib.error.HTTPError as exc:
            payload = _error_payload(exc)
            if exc.code != 429:
                return exc.code, payload
            remaining = deadline - clock()
            if remaining <= 0:
                return 429, payload
            wait = min(delay * (0.5 + random.random()), remaining)  # noqa: S311  (jitter, not crypto)
            print(f"dispatcher at capacity (429); retrying in {wait:.0f}s", flush=True)
            sleep(wait)
            retrying = True
            delay = min(delay * 2, 120.0)
            continue
        # A success with no readable result is a failure, not an empty success: a
        # connection that closes after the headers reads as b"" here, and the task has
        # probably run. Not retried, for the same reason as any post-admission failure.
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = None
        if not isinstance(payload, dict):
            raise ClientError(
                f"/run returned HTTP {status} without a JSON object body; the task may have run, but its result was lost"
            )
        return status, payload


def _error_payload(exc: urllib.error.HTTPError) -> dict:
    try:
        payload = json.loads(exc.read(MAX_RESPONSE_BYTES) or b"{}")
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def write_outputs(path: str, outputs: dict[str, str]) -> None:
    """Append to $GITHUB_OUTPUT, using a random heredoc delimiter so a report cannot end
    the value early and inject further outputs."""
    with open(path, "a", encoding="utf-8") as handle:
        for name, value in outputs.items():
            delimiter = f"EOF_{secrets.token_hex(16)}"
            handle.write(f"{name}<<{delimiter}\n{value}\n{delimiter}\n")


def main(env: dict | None = None, opener=None, token_opener=None) -> int:
    env = dict(os.environ) if env is None else env
    # The dispatcher is an in-cluster address and must never go through a proxy; the
    # token endpoint is GitHub's and may need whatever proxy the runner is configured with.
    opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}))
    token_opener = token_opener or urllib.request.build_opener()
    endpoint = (env.get("INPUT_ENDPOINT", "").strip() or DEFAULT_ENDPOINT).rstrip("/")
    try:
        body = build_body(env)
        max_wait = float(env.get("INPUT_MAX_WAIT", "900") or "900")
        if not math.isfinite(max_wait) or max_wait < 0:
            raise ClientError(f"input `max-wait` must be a finite number of seconds, got {max_wait}")
        status, payload = call_run(opener, token_opener, env, endpoint, body, max_wait)
    except (ClientError, ValueError) as exc:
        print(f"::error::{_command_data(exc)}", flush=True)
        return 1
    except (OSError, urllib.error.URLError) as exc:
        # Transport failure: not retried, because the task may have been admitted.
        detail = f"could not complete /run ({exc}); dispatcher health: {healthz(opener, endpoint)}"
        print(f"::error::{_command_data(detail)}", flush=True)
        return 1

    if status not in (200, 202):
        reason = payload.get("error", "no reason given")
        detail = f"/run returned HTTP {status}: {reason}; dispatcher health: {healthz(opener, endpoint)}"
        print(f"::error::{_command_data(detail)}", flush=True)
        return 1

    # One file per invocation: a job that calls the action twice keeps both results.
    fd, result_file = tempfile.mkstemp(prefix="agent-sandbox-", suffix=".json", dir=env.get("RUNNER_TEMP") or None)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    # The id is task-controlled on a waited run (the result is spread over it), so only a
    # well-formed one reaches an output or the log: a caller may interpolate the output
    # into a script.
    task_id = payload.get("task_id")
    well_formed = isinstance(task_id, str) and TASK_ID_RE.fullmatch(task_id) is not None
    outputs = {"task-id": task_id if well_formed else "", "result-file": result_file}
    if status == 200:
        outputs["report"] = str(payload.get("report", ""))
    output_path = env.get("GITHUB_OUTPUT")
    if output_path:
        write_outputs(output_path, outputs)

    errors = payload.get("errors") or {}
    errors = errors if isinstance(errors, dict) else {"result": errors}
    # A rejected proposal is a diagnostic, not a failed run: the effects that passed
    # screening are still valid, and failing here would stop the apply step.
    if status == 200 and "effects" in errors:
        print(
            f"::warning::{_command_data('some proposed effects were rejected: ' + str(errors['effects']))}", flush=True
        )
    fatal = {k: v for k, v in errors.items() if k != "effects"}
    if status == 200 and fatal:
        print(f"::error::{_command_data('the task reported errors: ' + json.dumps(fatal))}", flush=True)
        return 1
    if not well_formed:
        print("::error::/run returned no well-formed task id; the result was not trusted", flush=True)
        return 1
    print(f"task {task_id} {'finished' if status == 200 else 'accepted'}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
