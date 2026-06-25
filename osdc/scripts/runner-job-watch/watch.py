#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "clickhouse-connect>=0.8",
#   "requests>=2.32",
# ]
# ///
"""
Watch for failed jobs on runners matching a configurable name prefix (default
`lf-`, the OSDC LF cluster), fetch their logs from HUD's S3, ask Claude to
classify them, and append the infra-flagged ones to a markdown file that's
friendly to `tail -f`.

Run:
    cd ~/meta/agent_space/lf-runner-watch
    uv run watch.py            # foreground, Ctrl-C to stop
    tail -f watch.md           # in another terminal

State:
    ./.watch.classification.json  — dedup cache, never wiped automatically
    ./watch.md                    — append-only human-tailable log

Classifier instructions are reloaded from
    ./error_classification_instructions.md
on every classification call, so you can edit them without restarting.
"""

from __future__ import annotations

import contextlib
import fcntl
import gzip
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import time
import traceback
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_cleared_localhost_proxy = False
for _proxy_var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
    _val = os.environ.get(_proxy_var, "")
    if _val and ("localhost" in _val or "127.0.0.1" in _val):
        os.environ.pop(_proxy_var, None)
        _cleared_localhost_proxy = True
if _cleared_localhost_proxy:
    os.environ["NO_PROXY"] = "*"
    os.environ["no_proxy"] = "*"

import clickhouse_connect  # noqa: E402  (must come after proxy env scrub above)
import requests  # noqa: E402

HERE = Path(__file__).resolve().parent
STATE_FILE = HERE / ".watch.classification.json"
OUTPUT_FILE = HERE / "watch.md"
INFRA_OUTPUT_FILE = HERE / "watch.infra.md"
INSTRUCTIONS_FILE = HERE / "error_classification_instructions.md"
LOCK_FILE = HERE / ".watch.lock"

POLL_INTERVAL_SEC = 60
LOOKBACK_MINUTES = 60
LOG_TAIL_BYTES = 32 * 1024
ERROR_CONTEXT_LINES = 50
ERROR_MATCH_LIMIT = 30
SLICE_MAX_BYTES = 96 * 1024
CLASSIFIER_MODEL = "claude-opus-4-6[1m]"
CLASSIFIER_TIMEOUT_SEC = 180
CLASSIFIER_MAX_RETRIES = 3
LOG_FETCH_TIMEOUT_SEC = 20

CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "/usr/local/bin/claude_code/os/claude")
if not os.path.isfile(CLAUDE_BIN) or not os.access(CLAUDE_BIN, os.X_OK):
    _fallback = shutil.which("claude")
    if _fallback:
        CLAUDE_BIN = _fallback

CLASSIFIER_ENV = {
    "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1",
    "CLAUDE_CODE_FILE_READ_MAX_OUTPUT_TOKENS": "100000",
    "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "64000",
    "CLAUDE_CODE_EFFORT_LEVEL": "max",
    "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "95",
    "BASH_MAX_TIMEOUT_MS": "600000",
    "META_CLAUDE_CODE_RELEASE": "latest",
    "META_CLAUDE_USE_GCP_DIRECT": "1",
    "META_CLAUDE_USE_ANTHROPIC_DIRECT": "0",
    "AGENT_ENVIRONMENT": "true",
}

CLASSIFIER_ENV_PASSTHROUGH = (
    "HOME",
    "USER",
    "LOGNAME",
    "PATH",
    "TMPDIR",
    "LANG",
    "LC_ALL",
    "TERM",
    "NO_PROXY",
    "no_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "http_proxy",
    "https_proxy",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_CUSTOM_HEADERS",
    "ANTHROPIC_API_KEY",
)

CLASSIFIER_ENV_DENY_CLAUDE_CODE = frozenset(
    {
        "CLAUDE_CODE_CHILD_SESSION",
        "CLAUDE_CODE_CURRENT_SESSION_ID",
        "CLAUDE_CODE_CURRENT_TRANSCRIPT_PATH",
        "CLAUDE_CODE_EXECPATH",
        "CLAUDE_CODE_SESSION_ID",
        "CLAUDE_CODE_TEAMMATE_COMMAND",
        "CLAUDE_CODE_TMPDIR",
        "CLAUDE_CODE_ENTRYPOINT",
        "CLAUDE_LAUNCHER_SESSION_FILE",
    }
)

INTERNET_MODE_MARKER = ".claude/internet-mode-used_DO_NOT_REMOVE_MANUALLY_SECURITY_RISK"


def clear_internet_mode_markers(start: Path) -> None:
    d = start.resolve()
    while True:
        marker = d / INTERNET_MODE_MARKER
        try:
            marker.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass
        if d.parent == d:
            break
        d = d.parent


ERROR_PATTERNS = [
    re.compile(r"##\[error\]", re.IGNORECASE),
    re.compile(r"^error[: ]", re.IGNORECASE | re.MULTILINE),
    re.compile(r"\bERROR\b"),
    re.compile(r"\bFAILED\b"),
    re.compile(r"\bFatal\b", re.IGNORECASE),
    re.compile(r"Traceback \(most recent call last\):"),
    re.compile(r"\bAssertionError\b"),
    re.compile(r"\bException\b"),
]

MUST_INCLUDE_PATTERNS = [
    re.compile(r"\[OSDC\] Step script exited with code"),
    re.compile(r"This is a script/workflow error, not an infrastructure issue"),
    re.compile(r"Did you mean to set the id-token permission\?"),
    re.compile(r"Performing Test CXX_SVE256_FOUND - Failed"),
    re.compile(r"No SVE support on this machine\. Set BUILD_IGNORE_SVE_UNAVAILABLE"),
    re.compile(r"FindARM\.cmake:\d+"),
    re.compile(r"seemethere/download-artifact-s3"),
    re.compile(r"Container .* was OOMKilled"),
]

CH_QUERY = """
SELECT
    id,
    workflow_name,
    name,
    runner_name,
    html_url,
    log_url,
    completed_at
FROM default.workflow_job
WHERE runner_name LIKE %(runner_pat)s
  AND conclusion = 'failure'
  AND completed_at > now() - INTERVAL %(lookback)d MINUTE
  AND repository_full_name = 'pytorch/pytorch'
ORDER BY completed_at DESC
"""


def log(msg: str) -> None:
    print(f"[{datetime.now(UTC).strftime('%H:%M:%S')}] {msg}", flush=True)


def load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception as e:
        log(f"state file unreadable ({e}); starting fresh")
        return {}


def save_state(state: dict[str, Any]) -> None:
    tmp = STATE_FILE.with_suffix(".json.tmp")
    with tmp.open("w") as f:
        f.write(json.dumps(state, indent=2, sort_keys=True))
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(STATE_FILE)


def acquire_lock():
    lock_fd = LOCK_FILE.open("w")
    try:
        fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_fd.close()
        return None
    lock_fd.write(f"pid={os.getpid()} started={datetime.now(UTC).isoformat()}\n")
    lock_fd.flush()
    return lock_fd


def ch_client():
    return clickhouse_connect.get_client(
        host=os.environ["CLICKHOUSE_HOST"],
        port=int(os.environ.get("CLICKHOUSE_PORT", "8443")),
        username=os.environ["CLICKHOUSE_HUD_USER_USERNAME"],
        password=os.environ["CLICKHOUSE_HUD_USER_PASSWORD"],
        secure=True,
    )


def query_failed_jobs(client, runner_prefix: str) -> list[dict[str, Any]]:
    result = client.query(
        CH_QUERY,
        parameters={"lookback": LOOKBACK_MINUTES, "runner_pat": f"{runner_prefix}%"},
    )
    cols = result.column_names
    return [dict(zip(cols, row, strict=False)) for row in result.result_rows]


def fetch_log(url: str) -> str:
    r = requests.get(url, timeout=LOG_FETCH_TIMEOUT_SEC, stream=False)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code} fetching {url}")
    body = r.content
    if r.headers.get("Content-Encoding") == "gzip" or body[:2] == b"\x1f\x8b":
        with contextlib.suppress(gzip.BadGzipFile):
            body = gzip.decompress(body)
    try:
        return body.decode("utf-8", errors="replace")
    except Exception:
        return body.decode("latin-1", errors="replace")


def _find_must_include_lines(lines: list[str]) -> list[int]:
    hits: list[int] = []
    seen: set[int] = set()
    for pat in MUST_INCLUDE_PATTERNS:
        for i, line in enumerate(lines):
            if i in seen:
                continue
            if pat.search(line):
                seen.add(i)
                hits.append(i)
    hits.sort()
    return hits


def _render_must_include_block(lines: list[str], hits: list[int]) -> str:
    if not hits:
        return ""
    chunks: list[str] = []
    for i in hits:
        lo = max(0, i - 5)
        hi = min(len(lines), i + 6)
        chunks.append(f"--- line {i + 1} (+/- 5) ---\n" + "\n".join(lines[lo:hi]))
    return "=== TRUSTED SIGNALS (must-read, never truncated) ===\n" + "\n".join(chunks)


def slice_log(full: str) -> str:
    tail = full[-LOG_TAIL_BYTES:] if len(full) > LOG_TAIL_BYTES else full
    lines = full.splitlines()
    must_hits = _find_must_include_lines(lines)
    must_block = _render_must_include_block(lines, must_hits)
    match_lines: list[int] = []
    seen: set[int] = set()
    for pat in ERROR_PATTERNS:
        for i, line in enumerate(lines):
            if i in seen:
                continue
            if pat.search(line):
                seen.add(i)
                match_lines.append(i)
    match_lines.sort()
    if len(match_lines) > ERROR_MATCH_LIMIT:
        match_lines = match_lines[-ERROR_MATCH_LIMIT:]
    context_indices: set[int] = set()
    for i in match_lines:
        lo = max(0, i - ERROR_CONTEXT_LINES // 2)
        hi = min(len(lines), i + ERROR_CONTEXT_LINES // 2 + 1)
        context_indices.update(range(lo, hi))
    if not context_indices:
        body = f"=== TAIL ({len(tail)} bytes) ===\n{tail}"
    else:
        ranges: list[tuple[int, int]] = []
        cur_start = cur_end = None
        for i in sorted(context_indices):
            if cur_start is None:
                cur_start = cur_end = i
            elif i == cur_end + 1:
                cur_end = i
            else:
                ranges.append((cur_start, cur_end))
                cur_start = cur_end = i
        if cur_start is not None:
            ranges.append((cur_start, cur_end))
        chunks = []
        for lo, hi in ranges:
            chunks.append(f"=== lines {lo + 1}-{hi + 1} ===")
            chunks.append("\n".join(lines[lo : hi + 1]))
        context = "\n".join(chunks)
        body = (
            f"=== TAIL ({len(tail)} bytes) ===\n{tail}\n\n=== ERROR CONTEXT ({len(match_lines)} matches) ===\n{context}"
        )
    if len(body) > SLICE_MAX_BYTES:
        body = body[:SLICE_MAX_BYTES] + f"\n\n[... body truncated to {SLICE_MAX_BYTES} bytes ...]"
    if must_block:
        return must_block + "\n\n" + body
    return body


def classify(job: dict[str, Any], log_slice: str, full_log_path: Path) -> dict[str, Any]:
    instructions = INSTRUCTIONS_FILE.read_text()
    last_err: str | None = None
    for attempt in range(1, CLASSIFIER_MAX_RETRIES + 1):
        out_path = Path(f"/tmp/lf-classify-{secrets.token_hex(8)}.json")
        prompt = f"""{instructions}

---

You are classifying ONE failed CI job. Do your reasoning freely, but the
ONLY thing that matters for downstream consumption is the JSON file you
write at the end. Write a single JSON object — matching the schema in the
instructions above — to this exact path:

    {out_path}

Use the Write tool. The file must exist, be non-empty, and parse as JSON.
Do not write the JSON anywhere else.

## Job metadata
- id:            {job["id"]}
- workflow:      {job["workflow_name"]} / {job["name"]}
- runner_name:   {job["runner_name"]}
- html_url:      {job["html_url"]}
- completed_at:  {job["completed_at"]}

## Log slice (tail + trusted signals + error context)
```
{log_slice}
```

## Full job log (use ONLY if the slice is insufficient)
The complete job log is on disk at:

    {full_log_path}

If the slice already answers the question, do NOT read this file — it can
be many MB. Read it (or `Grep` it) only when:
  - the slice mentions an error you cannot map to a category, AND
  - you need surrounding context that was cut, OR
  - you want to confirm whether a different signal (OSDC hook self-tag,
    SVE256 trio, OIDC strings, OOMKilled message) appears anywhere in the
    full log before deciding category.

Prefer targeted `Grep` over a full Read.
"""
        cmd = [
            CLAUDE_BIN,
            "--verbose",
            "-p",
            prompt,
            "--model",
            CLASSIFIER_MODEL,
            "--allowedTools",
            "Write,Read,Grep",
        ]
        if sys.platform == "darwin":
            cmd[1:1] = ["--dangerously-disable-osx-sandbox"]
        env = {k: v for k, v in os.environ.items() if k in CLASSIFIER_ENV_PASSTHROUGH}
        for k, v in os.environ.items():
            if k.startswith("CLAUDE_CODE_") and k not in CLASSIFIER_ENV_DENY_CLAUDE_CODE:
                env[k] = v
        env.update(CLASSIFIER_ENV)
        env["ANTHROPIC_MODEL"] = CLASSIFIER_MODEL
        clear_internet_mode_markers(HERE)
        try:
            subprocess.run(
                cmd,
                timeout=CLASSIFIER_TIMEOUT_SEC,
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                env=env,
            )
        except subprocess.TimeoutExpired:
            last_err = f"classifier timed out after {CLASSIFIER_TIMEOUT_SEC}s"
            log(f"  classify attempt {attempt}: {last_err}")
            out_path.unlink(missing_ok=True)
            continue
        if not out_path.exists():
            last_err = f"output file {out_path} not created"
            log(f"  classify attempt {attempt}: {last_err}")
            continue
        raw = out_path.read_text().strip()
        out_path.unlink(missing_ok=True)
        if not raw:
            last_err = "output file empty"
            log(f"  classify attempt {attempt}: {last_err}")
            continue
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as e:
            last_err = f"invalid JSON: {e}"
            log(f"  classify attempt {attempt}: {last_err}; raw={raw[:200]!r}")
            continue
        if not isinstance(parsed, dict) or "category" not in parsed:
            last_err = f"missing 'category' field: {parsed!r}"
            log(f"  classify attempt {attempt}: {last_err}")
            continue
        return parsed
    return {
        "category": "unknown",
        "confidence": "low",
        "summary": f"classifier failed after {CLASSIFIER_MAX_RETRIES} attempts: {last_err}",
        "suggested_action": "investigate manually",
    }


def render(job: dict[str, Any], cls: dict[str, Any]) -> tuple[str, str | None]:
    ts = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    cat = str(cls.get("category", "unknown")).upper()
    conf = cls.get("confidence", "?")
    summary = cls.get("summary", "")
    action = cls.get("suggested_action", "")
    job_id = job["id"]
    workflow = f"{job['workflow_name']} / {job['name']}"
    runner = job["runner_name"]
    url = job["html_url"]
    infra_block = (
        f"## {ts} - {cat} ({conf}) - job {job_id}\n"
        f"- workflow: {workflow}\n"
        f"- runner:   {runner}\n"
        f"- html:     {url}\n"
        f"- summary:  {summary}\n"
        f"- action:   {action}\n"
        f"---\n"
    )
    if cat == "INFRA_ISSUE":
        return infra_block, infra_block
    short = f"- {ts} {cat:<12s} ({conf}) job {job_id} {workflow} -- {summary} ({url})\n"
    return short, None


def append_output(entry: str, infra_entry: str | None) -> None:
    with OUTPUT_FILE.open("a") as f:
        f.write(entry)
        f.flush()
    if infra_entry is not None:
        with INFRA_OUTPUT_FILE.open("a") as f:
            f.write(infra_entry)
            f.flush()


def is_hud_mirror_rate_limit(body: str) -> bool:
    if len(body) > 4096:
        return False
    stripped = body.strip()
    if not stripped.startswith("{") or not stripped.endswith("}"):
        return False
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return False
    if not isinstance(parsed, dict):
        return False
    msg = parsed.get("message", "")
    status = str(parsed.get("status", ""))
    return status == "403" and isinstance(msg, str) and "API rate limit exceeded" in msg


def process_one(job: dict[str, Any], state: dict[str, Any]) -> float | None:
    job_id = str(job["id"])
    if job_id in state:
        return None
    log(f"new failure job_id={job_id} runner={job['runner_name']}")
    try:
        full_log = fetch_log(job["log_url"])
    except Exception as e:
        log(f"  log fetch failed: {e}; skipping (will retry next poll if still in window)")
        return None
    if is_hud_mirror_rate_limit(full_log):
        log(f"  log is HUD mirror rate-limit stub ({len(full_log)} bytes); skipping (real log may appear later)")
        return None
    if not full_log.strip():
        log("  log empty; marking as unknown")
        cls = {
            "category": "unknown",
            "confidence": "low",
            "summary": "log was empty or unavailable",
            "suggested_action": "check job in GHA UI",
        }
        classify_secs = 0.0
    else:
        sliced = slice_log(full_log)
        full_path = Path(f"/tmp/lf-classify-fulllog-{job_id}-{secrets.token_hex(4)}.log")
        full_path.write_text(full_log)
        try:
            t0 = time.time()
            cls = classify(job, sliced, full_path)
            classify_secs = time.time() - t0
        finally:
            full_path.unlink(missing_ok=True)
    log(f"  -> {cls.get('category')} ({cls.get('confidence')}) in {classify_secs:.1f}s: {cls.get('summary', '')[:120]}")
    entry, infra_entry = render(job, cls)
    append_output(entry, infra_entry)
    state[job_id] = {
        "classification": cls,
        "posted_at": datetime.now(UTC).isoformat(),
        "runner_name": job["runner_name"],
        "classify_secs": round(classify_secs, 2),
    }
    save_state(state)
    return classify_secs


_stop = False


def _sig(_signum, _frame):
    global _stop
    _stop = True
    log("signal received, shutting down after this iteration")


DEFAULT_RUNNER_PREFIX = "lf-"


def main(runner_prefix: str) -> int:
    for var in (
        "CLICKHOUSE_HOST",
        "CLICKHOUSE_PORT",
        "CLICKHOUSE_HUD_USER_USERNAME",
        "CLICKHOUSE_HUD_USER_PASSWORD",
    ):
        if not os.environ.get(var):
            print(f"missing env var: {var}", file=sys.stderr)
            return 2
    if not INSTRUCTIONS_FILE.exists():
        print(f"missing instructions: {INSTRUCTIONS_FILE}", file=sys.stderr)
        return 2

    lock_fd = acquire_lock()
    if lock_fd is None:
        print(f"another watch.py instance holds {LOCK_FILE}; refusing to start", file=sys.stderr)
        return 3

    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)

    log(f"output:       {OUTPUT_FILE}")
    log(f"infra output: {INFRA_OUTPUT_FILE}")
    log(f"state:        {STATE_FILE}")
    log(f"instructions: {INSTRUCTIONS_FILE} (reloaded every classify)")
    log(f"model:        {CLASSIFIER_MODEL}")
    log(f"runner prefix: {runner_prefix!r}")
    log(f"poll every {POLL_INTERVAL_SEC}s, lookback {LOOKBACK_MINUTES}min")

    if not OUTPUT_FILE.exists():
        OUTPUT_FILE.write_text(f"# LF Runner Failures Watch\n\nStarted {datetime.now(UTC).isoformat()}\n\n")
    if not INFRA_OUTPUT_FILE.exists():
        INFRA_OUTPUT_FILE.write_text(
            f"# LF Runner Failures Watch — INFRA only\n\nStarted {datetime.now(UTC).isoformat()}\n\n"
        )

    client = ch_client()
    state = load_state()
    log(f"state has {len(state)} previously-seen jobs")

    while not _stop:
        try:
            jobs = query_failed_jobs(client, runner_prefix)
            new = [j for j in jobs if str(j["id"]) not in state]
            log(f"poll: {len(jobs)} failures in last {LOOKBACK_MINUTES}min, {len(new)} new")
            durations: list[float] = []
            categories: dict[str, int] = {}
            for j in new:
                if _stop:
                    break
                try:
                    d = process_one(j, state)
                    if d is not None:
                        durations.append(d)
                        cat = str(state[str(j["id"])]["classification"].get("category", "unknown"))
                        categories[cat] = categories.get(cat, 0) + 1
                except Exception:
                    log(f"process_one crashed for job {j.get('id')}:")
                    traceback.print_exc()
            if durations:
                avg = sum(durations) / len(durations)
                breakdown = ", ".join(f"{c}={n}" for c, n in sorted(categories.items(), key=lambda x: -x[1]))
                log(
                    f"  classified {len(durations)} jobs this iter: avg={avg:.1f}s min={min(durations):.1f}s max={max(durations):.1f}s (timeout={CLASSIFIER_TIMEOUT_SEC}s)"
                )
                log(f"  by category: {breakdown}")
        except Exception:
            log("poll crashed:")
            traceback.print_exc()
            try:
                client = ch_client()
            except Exception:
                log("clickhouse reconnect failed; will retry next poll")
        for _ in range(POLL_INTERVAL_SEC):
            if _stop:
                break
            time.sleep(1)

    log("clean exit")
    return 0


def _parse_runner_prefix(argv: list[str]) -> tuple[str, list[str]]:
    rest: list[str] = []
    prefix = DEFAULT_RUNNER_PREFIX
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--runner-prefix" and i + 1 < len(argv):
            prefix = argv[i + 1]
            i += 2
            continue
        if a.startswith("--runner-prefix="):
            prefix = a.split("=", 1)[1]
            i += 1
            continue
        rest.append(a)
        i += 1
    return prefix, rest


if __name__ == "__main__":
    runner_prefix, args = _parse_runner_prefix(sys.argv[1:])
    if args and args[0] == "--selftest":
        c = ch_client()
        rows = query_failed_jobs(c, runner_prefix)
        print(f"OK: ClickHouse {runner_prefix!r} query returned {len(rows)} rows")
        probe = c.query(
            "SELECT id, log_url FROM default.workflow_job "
            "WHERE conclusion = 'failure' AND repository_full_name = 'pytorch/pytorch' "
            "AND completed_at > now() - INTERVAL 6 HOUR ORDER BY completed_at DESC LIMIT 1"
        )
        if probe.result_rows:
            jid, lurl = probe.result_rows[0]
            print(f"OK: probe job_id={jid} url={lurl}")
            txt = fetch_log(lurl)
            print(f"OK: log fetch len={len(txt)} bytes")
            sliced = slice_log(txt)
            print(f"OK: slice len={len(sliced)} bytes (preview):")
            print(sliced[:500])
        sys.exit(0)
    if args and args[0] == "--like-probe":
        prefix = args[1] if len(args) > 1 else runner_prefix
        c = ch_client()
        q = """SELECT count() FROM default.workflow_job
               WHERE runner_name LIKE %(pat)s
                 AND completed_at > now() - INTERVAL 1 HOUR"""
        r = c.query(q, parameters={"pat": f"{prefix}%"})
        print(f"runner_name LIKE '{prefix}%' (last 1h): {r.result_rows[0][0]} rows")
        sys.exit(0)
    if args and args[0] == "--classify-probe":
        c = ch_client()
        probe = c.query(
            "SELECT id, workflow_name, name, runner_name, html_url, log_url, completed_at "
            "FROM default.workflow_job "
            "WHERE conclusion = 'failure' AND repository_full_name = 'pytorch/pytorch' "
            "AND completed_at > now() - INTERVAL 6 HOUR ORDER BY completed_at DESC LIMIT 1"
        )
        cols = probe.column_names
        job = dict(zip(cols, probe.result_rows[0], strict=False))
        print(f"probe job: {job['id']} {job['name']}")
        txt = fetch_log(job["log_url"])
        sliced = slice_log(txt)
        full_path = Path(f"/tmp/lf-classify-fulllog-{job['id']}-{secrets.token_hex(4)}.log")
        full_path.write_text(txt)
        print(f"slice {len(sliced)} bytes, full log at {full_path}, invoking classifier...")
        t0 = time.time()
        try:
            cls = classify(job, sliced, full_path)
        finally:
            full_path.unlink(missing_ok=True)
        print(f"classified in {time.time() - t0:.1f}s:")
        print(json.dumps(cls, indent=2))
        print("---rendered---")
        entry, infra_entry = render(job, cls)
        print(entry)
        if infra_entry is not None:
            print("---infra stream---")
            print(infra_entry)
        sys.exit(0)
    sys.exit(main(runner_prefix))
