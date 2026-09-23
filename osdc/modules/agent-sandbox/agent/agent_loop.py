"""The agent loop: let the model read the checkout through a few read-only tools.

v1 made one Bedrock call over the top-level listing, so the model could describe a
repository's shape and nothing inside it. This gives it three tools over the checked-out
commit — list a directory, read a file, search — and loops until it answers or a turn or
time budget runs out.

Every tool reads through git (`HEAD:<path>`), never the filesystem, so a path cannot
escape the checkout and a symlink in the repository cannot point one outside it. Tools
never raise: a bad argument or a git failure comes back to the model as text, which is
how it learns to correct itself.
"""

from __future__ import annotations

import contextlib
import http.client
import json
import os
import subprocess
import tempfile
import threading
import time

MAX_TURNS = 24
LOOP_DEADLINE_S = 600
# Per turn, and thinking counts toward it: a model that always thinks (Opus 5.5 runs
# adaptive thinking whether asked or not) can spend much of a small budget before it
# writes the answer. 16000 is the usual ceiling for a non-streaming call; the turn's
# time is bounded separately (MAX_CALL_S, and the loop deadline).
MAX_TOKENS = 16_000
# One model call, as a whole: headers, body and any error body. A non-streaming response
# arrives only when the turn is done, and a turn may think and write up to MAX_TOKENS.
# The invoke function uses it as its socket timeout (sandbox.invoke_model); the loop
# enforces it on the call's wall clock, and the loop deadline still bounds both.
MAX_CALL_S = 300
# Per tool call, and in total: every result is re-sent on every later turn.
MAX_TOOL_OUTPUT_BYTES = 32 * 1024
MAX_TRANSCRIPT_TOOL_BYTES = 768 * 1024
MAX_TOOL_CALLS = 120
# The whole serialized request, re-sent every turn. Far below Bedrock's 25 MB request
# limit, and a conversation this large has stopped being useful anyway.
MAX_REQUEST_BYTES = 4 * 1024 * 1024
# The byte budgets above do not bound tokens: source runs a few bytes per token, so 768 KiB
# of tool output alone can fill a 200K-token window. Tools therefore also stop on the
# model's own count (`usage` in each response) with room left for the next result and the
# answer. The window is the smallest of the models a manifest can select (Haiku 4.5).
CONTEXT_WINDOW_TOKENS = 200_000
CONTEXT_RESERVE_TOKENS = MAX_TOKENS + 16_384
# Every token is at least one byte, so counting one token per byte cannot undercount,
# whatever the content (dense or non-ASCII text can run well under 2 bytes per token).
MIN_BYTES_PER_TOKEN = 1
MAX_SEARCH_MATCHES = 100
MAX_PATH_CHARS = 4096
TOOL_TIMEOUT_S = 60
MAX_LINE_BYTES = 8 * 1024
# A model call is retried only for a failure another attempt can fix — throttling, a 5xx,
# a dropped connection — and only while enough of the deadline is left for a call to
# finish. Anything else (a 4xx, a malformed body) would fail the same way again.
MAX_CALL_RETRIES = 3
RETRY_BASE_S = 2.0
MIN_RETRY_REMAINING_S = 30.0
# Time is a budget like the others: tools are refused while this much is still left, so
# the model gets a turn to answer from what it has read. A tenth of a shorter limit.
ANSWER_RESERVE_S = 60.0

SYSTEM = (
    "You are a careful engineer working in a read-only checkout of a git repository. "
    "Use the tools to read the files you need before answering. Base every claim on what "
    "you have read, cite paths and line numbers, and say so plainly when the tools do not "
    "give you enough to answer. Never invent file contents."
)

TOOLS = [
    {
        "name": "list_dir",
        "description": "List a directory of the checkout. Directories end with '/'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory relative to the repo root; '' for the root."}
            },
        },
    },
    {
        "name": "read_file",
        "description": "Read a file of the checkout, with line numbers. Long files are cut; use start_line to page.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "start_line": {"type": "integer", "minimum": 1},
                "end_line": {"type": "integer", "minimum": 1},
            },
            "required": ["path"],
        },
    },
    {
        "name": "search",
        "description": "Search the checkout for a regular expression (git grep). Returns path:line:text matches.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "path": {"type": "string", "description": "Limit the search to this directory or file."},
            },
            "required": ["pattern"],
        },
    },
]


PROPOSE_EFFECT = {
    "name": "propose_effect",
    "description": (
        "Propose a write to the pull request under review: a comment, or a check run with a "
        "conclusion. Nothing is written by you; a separate trusted step checks and applies "
        "proposals after you finish. Propose each effect once, with your final text."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "effect": {"type": "string", "enum": []},
            "body": {"type": "string", "description": "Markdown: the comment, or the check run's summary."},
            "title": {"type": "string", "description": "check_run only."},
            "conclusion": {"type": "string", "description": "check_run only."},
        },
        "required": ["effect", "body"],
    },
}
MAX_PROPOSALS = 3
MAX_PROPOSAL_BYTES = 128 * 1024


class ToolError(ValueError):
    """A tool call the model should see as an error message, not a crash."""


class ModelHTTPError(RuntimeError):
    """An HTTP error from a model call, already summarised, with its status code."""

    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.status = status


# What a model call can raise that ends the loop with its progress kept: an HTTP error,
# a dropped connection or timeout (OSError), a truncated or reset response
# (HTTPException), and a body that does not parse (ValueError, KeyError, TypeError,
# RecursionError on deep nesting).
MODEL_CALL_ERRORS = (
    ModelHTTPError,
    OSError,
    http.client.HTTPException,
    ValueError,
    KeyError,
    TypeError,
    RecursionError,
)


def _transient(exc: BaseException) -> bool:
    if isinstance(exc, ModelHTTPError):
        return exc.status == 429 or exc.status >= 500
    return isinstance(exc, (OSError, http.client.HTTPException))


def _read_until(out, stop: bytes, limit: int) -> bytes | None:
    """Bytes up to `stop` (consumed, not returned), at most `limit` of them; None at a
    clean end of file."""
    buf = bytearray()
    while len(buf) <= limit:
        byte = out.read(1)
        if not byte:
            return bytes(buf) if buf else None
        if byte == stop:
            return bytes(buf)
        buf += byte
    raise ToolError("unexpected search output")


def _argument_text(value, what: str) -> str:
    """A tool string argument git can receive: no NUL, and encodable (no lone surrogates)."""
    if not isinstance(value, str) or "\0" in value:
        raise ToolError(f"{what} must be a string without NUL characters")
    try:
        value.encode()
    except UnicodeEncodeError:
        raise ToolError(f"{what} is not valid text") from None
    return value


def _display_name(name: bytes) -> str:
    """A path as the model sees it. A name that is not UTF-8 cannot be passed back to the
    tools faithfully — its escaped form could name a different, literal file — so it is
    shown escaped and marked unreadable instead of silently aliasing another path."""
    try:
        return name.decode()
    except UnicodeDecodeError:
        return name.decode(errors="backslashreplace") + " [non-UTF-8 name; not readable with these tools]"


def _cut(text: str, limit: int) -> str:
    """At most `limit` bytes of `text`, cut on a character boundary."""
    return text.encode()[: max(0, limit)].decode(errors="ignore")


def _clip(text: str) -> str:
    limit = MAX_TOOL_OUTPUT_BYTES
    encoded = text.encode()
    if len(encoded) <= limit:
        return text
    return encoded[:limit].decode(errors="ignore") + "\n[output truncated]"


class RepoTools:
    """Read-only tools over the commit checked out at `dest`.

    Git writes to a scratch file, never to memory: a large blob or a broad search then
    costs disk, and only what fits the output budget is read back. The scratch files are
    NAMED, in a directory beside the checkout: an unlinked temporary file can escape the
    kubelet's ephemeral-storage accounting, a named one cannot.
    """

    def __init__(self, dest: str, effects: list | None = None):
        self.dest = dest
        # Writes the run may propose, from the manifest via SANDBOX_EFFECTS.
        self.effects = {e["effect"]: e for e in (effects or []) if isinstance(e, dict) and "effect" in e}
        self.proposals: list[dict] = []
        self.timeout = TOOL_TIMEOUT_S
        self.scratch = os.path.join(os.path.dirname(os.path.abspath(dest)), ".agent-scratch")

    @contextlib.contextmanager
    def _scratch_file(self):
        os.makedirs(self.scratch, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=self.scratch) as handle:
            yield handle

    def _git(self, args: list[str], out) -> subprocess.CompletedProcess:
        """Run git with stdout to `out`. stderr goes to a scratch file too — a repository
        can make git warn once per line of its .gitattributes — and only its first 500
        bytes come back, as `stderr` on the result."""
        with self._scratch_file() as err:
            done = subprocess.run(
                # Paths are literal names, never pathspec patterns (`[a]x.py` means that file).
                ["git", "--literal-pathspecs", *args],
                cwd=self.dest,
                stdout=out,
                stderr=err,
                timeout=self.timeout,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            )
            err.seek(0)
            done.stderr = err.read(500)
        return done

    @staticmethod
    def _path(value, allow_empty: bool) -> str:
        if value is None and allow_empty:
            return ""
        if not isinstance(value, str) or len(value) > MAX_PATH_CHARS:
            raise ToolError("path must be a string")
        _argument_text(value, "path")
        # Only slashes are normalized: whitespace is part of a file name.
        path = value.strip("/")
        while path.startswith("./"):
            path = path[2:]
        if path in ("", ".") and allow_empty:
            return ""
        if not path or path == "." or any(part in ("..", "") for part in path.split("/")):
            raise ToolError(f"invalid path {value!r}: use a path relative to the repository root")
        return path

    def list_dir(self, path=None) -> str:
        path = self._path(path, allow_empty=True)
        with self._scratch_file() as out:
            if self._git(["ls-tree", "-z", f"HEAD:{path}" if path else "HEAD"], out).returncode != 0:
                raise ToolError(f"no such directory: {path!r}")
            size = out.tell()
            out.seek(0)
            raw = out.read(MAX_TOOL_OUTPUT_BYTES)
        cut = size > len(raw)
        records = raw.split(b"\0")
        if cut:
            records = records[:-1]  # the last record may be incomplete
        lines = []
        for record in records:
            if record:
                meta, _, name = record.partition(b"\t")
                display = _display_name(name)
                lines.append(f"{display}/" if meta.split(b" ")[1:2] == [b"tree"] else display)
        if cut:
            lines.append("[listing cut; list a subdirectory or use search]")
        return _clip("\n".join(lines) or "(empty directory)")

    def read_file(self, path=None, start_line=1, end_line=None) -> str:
        path = self._path(path, allow_empty=False)
        if not isinstance(start_line, int) or isinstance(start_line, bool) or start_line < 1:
            raise ToolError("start_line must be a positive integer")
        if end_line is not None and (
            not isinstance(end_line, int) or isinstance(end_line, bool) or end_line < start_line
        ):
            raise ToolError("end_line must be an integer no smaller than start_line")
        # One deadline for git and for the scan that follows it.
        deadline = time.monotonic() + self.timeout
        with self._scratch_file() as out:
            if self._git(["cat-file", "blob", f"HEAD:{path}"], out).returncode != 0:
                raise ToolError(f"no such file: {path!r}")
            out.seek(0)
            shown, _, number, status = self._scan(out, start_line, end_line, deadline)
        total = f"{number} lines" if status == "complete" else f"at least {number} lines"
        notes = {
            "budget": f"\n[output budget reached after line {start_line + len(shown) - 1}; page with start_line]",
            "timeout": "\n[stopped: the read timed out]",
        }
        return f"{path} ({total})\n" + "\n".join(shown) + notes.get(status, "")

    @staticmethod
    def _scan(out, start_line, end_line, deadline):
        """Stream the file line by line — paging deep into a large file stays cheap, one
        enormous line is cut rather than read whole, and nothing past the requested range
        or the output budget is read at all. Returns (lines, bytes, last line number,
        status) with status complete, range, budget or timeout."""
        shown, used, number = [], 0, 0
        while True:
            if time.monotonic() > deadline:
                return shown, used, number, "timeout"
            raw = out.readline(MAX_LINE_BYTES)
            if not raw:
                return shown, used, number, "complete"
            number += 1
            long_line = len(raw) == MAX_LINE_BYTES and not raw.endswith(b"\n")
            while long_line:
                if time.monotonic() > deadline:
                    return shown, used, number, "timeout"
                rest = out.readline(MAX_LINE_BYTES)
                if not rest or rest.endswith(b"\n"):
                    break
            if number < start_line:
                continue
            body = raw.rstrip(b"\n").decode(errors="backslashreplace")
            line = f"{number}: {body}" + (" [line cut]" if long_line else "")
            room = MAX_TOOL_OUTPUT_BYTES - used - 1
            if len(line.encode()) > room:
                if not shown:
                    # Escaping can grow a line past the whole budget; show what fits
                    # rather than nothing, or the line could never be read.
                    shown.append(_cut(line, room - len(" [line cut]")) + " [line cut]")
                return shown, used, number, "budget"
            shown.append(line)
            used += len(line.encode()) + 1
            if end_line is not None and number >= end_line:
                return shown, used, number, "range"

    def search(self, pattern=None, path=None) -> str:
        if not isinstance(pattern, str) or not pattern or len(pattern) > 1000:
            raise ToolError("pattern must be a non-empty string")
        _argument_text(pattern, "pattern")
        path = self._path(path, allow_empty=True)
        # -z prints each path verbatim and ends it, and the line number, with NUL: the
        # default output quotes a non-ASCII or unusual name (`"caf\303\251.txt"`), which
        # read_file could not open.
        args = ["grep", "-n", "-z", "-I", "-E", f"--max-count={MAX_SEARCH_MATCHES}", "-e", pattern, "HEAD", "--"]
        with self._scratch_file() as out:
            done = self._git([*args, path] if path else args, out)
            if done.returncode == 1:
                return "no matches"
            if done.returncode != 0:
                raise ToolError(f"search failed: {done.stderr.decode(errors='replace')}")
            out.seek(0)
            lines, more, per_file = self._matches(out)
        if more:
            lines.append("[more matches not shown; narrow the pattern or the path]")
        if any(count >= MAX_SEARCH_MATCHES for count in per_file.values()):
            lines.append(f"[at most {MAX_SEARCH_MATCHES} matches per file are shown]")
        # Bounded again after decoding: escaping can multiply a byte by four.
        return _clip("\n".join(lines))

    @staticmethod
    def _matches(out):
        """`path:line:text` lines from `git grep -z -n` records (`HEAD:<path>\\0<line>\\0
        <text>\\n`), streamed until the output budget is used. Paths are named the way
        list_dir names them, and each text is cut to MAX_LINE_BYTES first, so one enormous
        line cannot hide its own location or the matches after it. Returns (lines, more,
        matches per file)."""
        lines, used, per_file = [], 0, {}
        while True:
            name = _read_until(out, b"\0", 2 * MAX_PATH_CHARS)
            if name is None:
                return lines, False, per_file
            number = _read_until(out, b"\0", 32) or b"?"
            raw = out.readline(MAX_LINE_BYTES)
            long_line = len(raw) == MAX_LINE_BYTES and not raw.endswith(b"\n")
            while long_line:
                rest = out.readline(MAX_LINE_BYTES)
                if not rest or rest.endswith(b"\n"):
                    break
            display = _display_name(name.removeprefix(b"HEAD:"))
            text = raw.rstrip(b"\n").decode(errors="backslashreplace")
            if len(text.encode()) > MAX_LINE_BYTES:
                # Escaping can make each invalid byte four characters; cut what renders,
                # so the line keeps its location instead of filling the budget alone.
                text, long_line = _cut(text, MAX_LINE_BYTES), True
            line = f"{display}:{number.decode(errors='replace')}:{text}" + (" [line cut]" if long_line else "")
            # Room is kept for the two notes search() may append, so they survive _clip.
            if used + len(line.encode()) + 1 > MAX_TOOL_OUTPUT_BYTES - 128:
                return lines, True, per_file
            lines.append(line)
            used += len(line.encode()) + 1
            per_file[display] = per_file.get(display, 0) + 1

    def specs(self) -> list[dict]:
        """The tools offered to the model: the read tools, plus propose_effect when the
        manifest allows at least one write."""
        if not self.effects:
            return TOOLS
        propose = json.loads(json.dumps(PROPOSE_EFFECT))
        propose["input_schema"]["properties"]["effect"]["enum"] = sorted(self.effects)
        return [*TOOLS, propose]

    def propose_effect(self, effect=None, body=None, title=None, conclusion=None) -> str:
        """Record a proposal. Checked here only enough to tell the model early; the
        dispatcher re-checks every proposal against the Grant."""
        spec = self.effects.get(effect)
        if spec is None:
            raise ToolError(f"effect must be one of {sorted(self.effects)}")
        if not isinstance(body, str) or not body.strip():
            raise ToolError("body must be non-empty text")
        if len(body.encode()) > spec.get("max_bytes", MAX_PROPOSAL_BYTES):
            raise ToolError(f"body is longer than {spec.get('max_bytes')} bytes; shorten it")
        if effect == "check_run" and conclusion not in spec.get("conclusions", []):
            raise ToolError(f"conclusion must be one of {spec.get('conclusions', [])}")
        used = sum(len(json.dumps(p)) for p in self.proposals)
        proposal = {
            k: v
            for k, v in {"effect": effect, "body": body, "title": title, "conclusion": conclusion}.items()
            if v is not None
        }
        if len(self.proposals) >= MAX_PROPOSALS or used + len(json.dumps(proposal)) > MAX_PROPOSAL_BYTES:
            raise ToolError("no more effects can be proposed in this run")
        self.proposals.append(proposal)
        return f"proposed {effect} ({len(self.proposals)} of at most {MAX_PROPOSALS}); it will be checked and applied after you finish"

    def run(self, name: str, arguments) -> str:
        tool = {"list_dir": self.list_dir, "read_file": self.read_file, "search": self.search}.get(name)
        if name == "propose_effect" and self.effects:
            tool = self.propose_effect
        if tool is None:
            return f"error: unknown tool {name!r}"
        if not isinstance(arguments, dict):
            return "error: tool input must be an object"
        try:
            return tool(**arguments)
        except TypeError:
            return f"error: unexpected arguments for {name}: {sorted(arguments)}"
        except ToolError as exc:
            return f"error: {exc}"
        except subprocess.TimeoutExpired:
            return f"error: {name} timed out"
        except (OSError, ValueError) as exc:
            # Anything else a bad argument can provoke still goes back to the model, so
            # one malformed call cannot end the review.
            return f"error: {name} failed: {exc}"


# Required string fields per content block type. Thinking blocks are replayed unchanged:
# the Messages API needs them back, signature and all, to continue a tool-use turn.
_BLOCK_FIELDS = {
    "text": ("text",),
    "tool_use": ("id", "name"),
    "thinking": ("thinking",),
    "redacted_thinking": ("data",),
}


def _well_formed(content) -> bool:
    """Every block is a known type with its required fields, and tool-use ids are
    present and unique — a missing field must not be read as an empty one."""
    if not isinstance(content, list):
        return False
    ids = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") not in _BLOCK_FIELDS:
            return False
        if not all(isinstance(block.get(f), str) for f in _BLOCK_FIELDS[block["type"]]):
            return False
        if block["type"] == "tool_use":
            if not block["id"] or not isinstance(block.get("input", {}), dict):
                return False
            ids.append(block["id"])
    return len(ids) == len(set(ids))


def prompt_budget_bytes() -> int:
    """Largest first prompt that fits the window at one token per byte, with the system
    prompt, tool definitions and the answer's reserve accounted for. There is no `usage`
    before the first call, so this floor is the only bound on it."""
    fixed = len(json.dumps({"system": SYSTEM, "tools": TOOLS}).encode()) + 1024
    return (CONTEXT_WINDOW_TOKENS - CONTEXT_RESERVE_TOKENS) * MIN_BYTES_PER_TOKEN - fixed


class _Overran(Exception):
    """A model call still running when the loop's wall-clock deadline passed."""


def _call_with_retries(invoke, model: str, fields: dict, remaining: float, deadline: float, clock, sleep) -> dict:
    """`_call_within`, retrying a transient failure with backoff inside the deadline.

    A loop makes up to MAX_TURNS calls, so one throttling response or proxy restart late
    in a run would otherwise throw away every turn before it. `remaining` is what the
    caller just measured; a retry measures again.
    """
    attempt = 0
    while True:
        try:
            return _call_within(invoke, model, fields, remaining)
        except _Overran:
            raise
        except MODEL_CALL_ERRORS as exc:
            attempt += 1
            wait = RETRY_BASE_S * 2 ** (attempt - 1)
            if attempt > MAX_CALL_RETRIES or not _transient(exc) or deadline - clock() - wait < MIN_RETRY_REMAINING_S:
                raise
            sleep(wait)
            remaining = deadline - clock()
            # A sleep can overrun; a retry started without room to finish only delays
            # the report. The original error stands.
            if remaining < MIN_RETRY_REMAINING_S:
                raise


def _call_within(invoke, model: str, fields: dict, remaining: float) -> dict:
    """`invoke`, bounded by wall-clock time as a whole: MAX_CALL_S, or what is left of
    the loop when that is less.

    `timeout` inside invoke is a per-socket-operation limit, so waiting for headers, a
    slow body and an error body can each take nearly all of it. The call runs on a daemon
    thread and is abandoned at the limit; the task prints its result and exits, which
    ends the thread with the process. Running out of the loop's time raises _Overran;
    running out of the call's own limit with loop time left is a TimeoutError, which the
    caller may retry.
    """
    limit = min(remaining, MAX_CALL_S)
    box: dict = {}

    def target():
        try:
            box["value"] = invoke(model, fields, timeout=limit)
        except BaseException as exc:  # re-raised on the caller's thread
            box["error"] = exc

    thread = threading.Thread(target=target, name="model-call", daemon=True)
    thread.start()
    thread.join(max(0.0, limit))
    if thread.is_alive():
        if limit < remaining:
            raise TimeoutError(f"model call still running after {MAX_CALL_S}s")
        raise _Overran
    if "error" in box:
        raise box["error"]
    return box["value"]


def _context_tokens(response: dict, fields: dict) -> int:
    """How much of the window the next request starts with: the model's own count of this
    request plus its reply, or a deliberately high byte estimate when `usage` is missing."""
    usage = response.get("usage")
    keys = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")
    if isinstance(usage, dict) and isinstance(usage.get("input_tokens"), int):
        return sum(v for k in keys if isinstance(v := usage.get(k), int) and not isinstance(v, bool) and v > 0)
    size = len(json.dumps(fields)) + len(json.dumps(response.get("content")))
    return size // MIN_BYTES_PER_TOKEN


def run_agent(
    invoke,
    model: str,
    prompt: str,
    tools: RepoTools,
    clock=time.monotonic,
    time_limit_s: float = LOOP_DEADLINE_S,
    sleep=time.sleep,
) -> dict:
    """Loop model -> tools -> model until the model answers. Returns report, turns,
    tool_calls, and `error` when the loop did not end in a complete answer.

    Also `tools_refused`, naming the budget, when a tool call was refused and the model
    answered from what it had already read: the answer stands, but a caller can tell it
    from one written after complete reading. And `model_error` when a model call failed
    for good (after retrying a transient failure) — the turns and tool calls so far are
    kept, so the result shows how far the run got.

    `invoke(model, fields, timeout)` is one Messages API call returning the parsed
    response; its timeout is capped by what is left of the deadline. Only
    `end_turn`/`stop_sequence` with text is an answer; `max_tokens` is a truncated one,
    and anything else is an error, so a partial report is never passed off as complete.

    `time_limit_s` is at most LOOP_DEADLINE_S; the caller lowers it to what is left of
    the task's own deadline, so the loop reports before Kubernetes kills the pod.
    """
    time_limit_s = min(time_limit_s, LOOP_DEADLINE_S)
    deadline = clock() + time_limit_s
    answer_reserve = min(ANSWER_RESERVE_S, time_limit_s / 10)
    messages = [{"role": "user", "content": [{"type": "text", "text": prompt}]}]
    text, calls, spent = "", 0, 0
    # Set once any call was refused for budget: the model was told to answer, and gets one
    # more turn to do it. Asking for tools again ends the loop, so refusals cannot keep
    # growing the conversation toward the window. `refused` names the first budget hit.
    final, refused = False, ""

    def done(turns, error=None):
        outcome = {"report": text, "turns": turns, "tool_calls": calls}
        if error:
            outcome["error"] = error
        if refused:
            outcome["tools_refused"] = refused
        return outcome

    for turn in range(1, MAX_TURNS + 1):
        remaining = deadline - clock()
        if remaining < 1:
            return done(turn - 1, f"time limit of {max(0, int(time_limit_s))}s reached")
        fields = {"system": SYSTEM, "messages": messages, "tools": tools.specs(), "max_tokens": MAX_TOKENS}
        if len(json.dumps(fields)) > MAX_REQUEST_BYTES:
            return done(turn - 1, f"the conversation grew past {MAX_REQUEST_BYTES} bytes")
        if turn == 1 and len(prompt.encode()) > prompt_budget_bytes():
            return done(0, "the prompt is too large for the model's context window")
        try:
            response = _call_with_retries(invoke, model, fields, remaining, deadline, clock, sleep)
        except _Overran:
            return done(turn - 1, f"time limit of {max(0, int(time_limit_s))}s reached during a model call")
        except MODEL_CALL_ERRORS as exc:
            outcome = done(turn - 1)
            outcome["model_error"] = str(exc)
            return outcome
        content = response.get("content") or []
        if not _well_formed(content):
            return done(turn, "unexpected model response (malformed content)")
        # Before the reply joins `messages`: `fields` shares that list, and the fallback
        # estimate adds the reply itself.
        room = CONTEXT_WINDOW_TOKENS - CONTEXT_RESERVE_TOKENS - _context_tokens(response, fields)
        messages.append({"role": "assistant", "content": content})
        room_bytes = room * MIN_BYTES_PER_TOKEN
        text = "".join(c.get("text", "") for c in content if c.get("type") == "text")
        uses = [c for c in content if c.get("type") == "tool_use"]
        stop = response.get("stop_reason")
        if stop in ("end_turn", "stop_sequence"):
            if uses:
                return done(turn, "the model ended its turn with tool calls still pending")
            return done(turn) if text.strip() else done(turn, "the model returned no answer")
        if stop == "max_tokens":
            return done(turn, f"the answer was cut at {MAX_TOKENS} tokens")
        if stop != "tool_use" or not uses:
            return done(turn, f"unexpected model response (stop_reason={stop!r})")
        if final:
            return done(turn, "the model kept calling tools after its budget ran out")
        results = []
        for use in uses:
            remaining = deadline - clock()
            # A result is at most MAX_TOOL_OUTPUT_BYTES plus a short note, so a call runs
            # only while one more worst-case result still fits the context window.
            no_room = room_bytes < MAX_TOOL_OUTPUT_BYTES + 1024
            # Turns are a budget too: the last turn is kept for the answer.
            limit = (
                "time"
                if remaining <= answer_reserve
                else f"{MAX_TURNS} turns"
                if turn >= MAX_TURNS - 1
                else f"{MAX_TOOL_CALLS} tool calls"
                if calls >= MAX_TOOL_CALLS
                else "tool output"
                if spent >= MAX_TRANSCRIPT_TOOL_BYTES
                else "context window"
                if no_room
                else ""
            )
            if limit:
                output = "error: tool budget exhausted — answer now with what you have read"
                final = True
                refused = refused or limit
            else:
                calls += 1
                # A read may not spend the answer reserve: it is bounded by what is left
                # before it, not by what is left of the loop.
                tools.timeout = max(1, min(TOOL_TIMEOUT_S, int(remaining - answer_reserve)))
                output = tools.run(use.get("name"), use.get("input"))
                spent += len(output.encode())
            room_bytes -= len(output.encode()) + 256  # the result and its framing
            results.append({"type": "tool_result", "tool_use_id": use.get("id", ""), "content": output})
        messages.append({"role": "user", "content": results})
    return done(MAX_TURNS, f"turn limit of {MAX_TURNS} reached")
