"""Tests for the sandbox agent worker (server.py).

The worker is stdlib-only — it reaches Bedrock through the sigv4 proxy over plain
HTTP — so nothing needs stubbing. Everything runs for real: `clone_repo` against a
real git repo (redirected off the network), `invoke_bedrock` against a fake sigv4
proxy on a real socket, and the HTTP surface against a real socket.
"""

import http.client
import io
import json
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

import pytest
import server


def _git_repo(path, entries):
    """A real git repo with `entries` (paths ending in / become directories)."""
    path.mkdir(parents=True, exist_ok=True)
    for entry in entries:
        target = path / entry.rstrip("/")
        if entry.endswith("/"):
            target.mkdir(parents=True, exist_ok=True)
            (target / "keep.txt").write_text("x\n")
        else:
            target.write_text("x\n")
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=True, env={**env, "PATH": "/usr/bin:/bin"})
    return path


class TestCloneRepo:
    """`clone_repo` hardcodes https://github.com/<repo>.git (it only ever clones
    public repos). Point that URL at a local repo with git's `insteadOf` rewrite so
    the real clone path is exercised without touching the network."""

    @pytest.fixture
    def local_github(self, tmp_path, monkeypatch):
        origin = _git_repo(tmp_path / "origin" / "org" / "repo.git", ["README.md", "setup.py", "torch/"])
        gitconfig = tmp_path / "gitconfig"
        gitconfig.write_text(
            f'[url "{(tmp_path / "origin").as_uri()}/"]\n\tinsteadOf = https://github.com/\n',
        )
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
        return origin

    def test_counts_tracked_files(self, local_github, tmp_path):
        # 3 tracked files: README.md, setup.py, torch/keep.txt
        assert server.clone_repo("org/repo", "main", str(tmp_path / "dest")) == 3

    def test_missing_ref_raises(self, local_github, tmp_path):
        with pytest.raises(subprocess.CalledProcessError):
            server.clone_repo("org/repo", "no-such-branch", str(tmp_path / "dest"))

    def test_terminal_prompts_stay_disabled(self, local_github, tmp_path, monkeypatch):
        """A credential prompt would hang the worker forever instead of failing."""
        captured = {}
        real_run = subprocess.run

        def spy(cmd, **kwargs):
            captured.setdefault("env", kwargs.get("env"))
            return real_run(cmd, **kwargs)

        monkeypatch.setattr(server.subprocess, "run", spy)
        server.clone_repo("org/repo", "main", str(tmp_path / "dest"))
        assert captured["env"]["GIT_TERMINAL_PROMPT"] == "0"


class TestTopLevelEntries:
    def test_marks_directories_with_slash(self, tmp_path):
        repo = _git_repo(tmp_path / "repo", ["README.md", "setup.py", "torch/"])
        assert sorted(server.top_level_entries(str(repo))) == ["README.md", "setup.py", "torch/"]

    def test_lists_only_top_level(self, tmp_path):
        repo = _git_repo(tmp_path / "repo", ["aten/", "torch/"])
        entries = server.top_level_entries(str(repo))
        assert entries == ["aten/", "torch/"], "nested files must not appear as top-level entries"

    def test_raises_outside_a_repo(self, tmp_path):
        with pytest.raises(subprocess.CalledProcessError):
            server.top_level_entries(str(tmp_path))


class TestBuildPrompt:
    def test_includes_the_real_listing(self):
        prompt = server.build_prompt("pytorch/pytorch", "main", "List the files.", 21952, ["README.md", "torch/"])
        assert "pytorch/pytorch" in prompt
        assert "21952 tracked files" in prompt
        assert "README.md" in prompt
        assert "torch/" in prompt
        assert "List the files." in prompt

    def test_forbids_guessing(self):
        """Without this the model invents a plausible listing and the canary's
        'Bedrock returned a report' assertion proves nothing."""
        prompt = server.build_prompt("r", "main", "t", 1, ["a"])
        assert "do not invent paths" in prompt

    def test_omits_the_listing_section_when_empty(self):
        prompt = server.build_prompt("r", "main", "t", 0, [])
        assert "Top-level entries" not in prompt


@pytest.fixture
def fake_sigv4_proxy(monkeypatch):
    """Stand in for aws-sigv4-proxy: record what the worker sent, reply with a
    Bedrock-shaped body. The real proxy adds the SigV4 signature — the worker
    deliberately sends an unsigned request, which is the whole point of the
    design, so there is no credential here to assert on."""
    seen = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            seen["path"] = self.path
            seen["headers"] = dict(self.headers)
            seen["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            payload = json.dumps(seen.get("reply", {"content": [{"type": "text", "text": "the report"}]})).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *a):
            pass

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    Thread(target=httpd.serve_forever, daemon=True).start()
    monkeypatch.setattr(server, "SIGV4_PROXY", f"127.0.0.1:{httpd.server_address[1]}")
    yield seen
    httpd.shutdown()
    httpd.server_close()


class TestInvokeBedrock:
    def test_posts_an_unsigned_messages_api_request_to_the_proxy(self, fake_sigv4_proxy):
        model = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
        assert server.invoke_bedrock(model, "hello") == "the report"

        assert fake_sigv4_proxy["path"] == f"/model/{model}/invoke"
        body = fake_sigv4_proxy["body"]
        assert body["anthropic_version"] == "bedrock-2023-05-31"
        assert body["messages"] == [{"role": "user", "content": [{"type": "text", "text": "hello"}]}]

    def test_sets_the_bedrock_host_header_for_the_proxy_to_sign(self, fake_sigv4_proxy):
        """aws-sigv4-proxy signs for the upstream named in Host; without it the
        request would be signed for (and sent to) the wrong service."""
        server.invoke_bedrock("m", "hello")
        assert fake_sigv4_proxy["headers"]["Host"] == f"bedrock-runtime.{server.REGION}.amazonaws.com"

    def test_carries_no_credential(self, fake_sigv4_proxy):
        """The worker must never hold or send AWS credentials — signing is the
        proxy's job."""
        server.invoke_bedrock("m", "hello")
        sent = {k.lower() for k in fake_sigv4_proxy["headers"]}
        assert "authorization" not in sent
        assert not any(h.startswith("x-amz-security-token") for h in sent)

    def test_empty_content_yields_empty_report(self, fake_sigv4_proxy):
        fake_sigv4_proxy["reply"] = {"content": []}
        assert server.invoke_bedrock("m", "hello") == ""

    def test_model_is_one_percent_encoded_path_segment(self, fake_sigv4_proxy):
        """An ARN is a documented model identifier and contains "/" — unencoded it
        splits the path and the request stops naming a model invoke at all."""
        arn = "arn:aws:bedrock:us-east-1:123456789012:inference-profile/us.anthropic.claude-haiku-4-5-v1:0"
        server.invoke_bedrock(arn, "hello")
        path = fake_sigv4_proxy["path"]
        assert path.endswith("/invoke")
        assert path.count("/") == 3, f"model id must be a single path segment, got {path}"
        assert "inference-profile%2F" in path
        # ":" stays as sent today — a legal path character, and every model id in
        # use ends in "…-v1:0".
        assert path.startswith("/model/arn:aws:bedrock:")

    def test_model_cannot_steer_the_path_the_proxy_signs(self, fake_sigv4_proxy):
        """`model` comes from an unauthenticated request body, and the proxy runs
        with no --name: it signs and forwards whatever path it is handed."""
        server.invoke_bedrock("../../async-invoke#", "hello")
        assert fake_sigv4_proxy["path"] == "/model/..%2F..%2Fasync-invoke%23/invoke"


class TestReadBounded:
    class _Trickle:
        """A body that never ends — one byte per read, as a stalled proxy would."""

        def read(self, _size):
            return b"x"

    def test_deadline_stops_a_trickling_response(self):
        """BEDROCK_TIMEOUT_S is urllib's per-operation socket timeout, not a wall
        clock: without a deadline this holds the single task slot indefinitely."""
        with pytest.raises(TimeoutError):
            server._read_bounded(self._Trickle(), server.MAX_RESPONSE_BYTES, time.monotonic() - 1)

    def test_oversized_response_is_rejected(self):
        with pytest.raises(ValueError, match="exceeded"):
            server._read_bounded(self._Trickle(), 4, time.monotonic() + 30)

    def test_reads_to_end_of_body(self):
        chunks = iter([b"abc", b"def", b""])

        class Body:
            def read(self, _size):
                return next(chunks)

        assert server._read_bounded(Body(), 1024, time.monotonic() + 30) == b"abcdef"


class TestBedrockErrorSummary:
    def _http_error(self, code, headers, body):
        return urllib.error.HTTPError("http://proxy/model/m/invoke", code, "Forbidden", headers, io.BytesIO(body))

    def test_error_code_from_body(self):
        """`str(HTTPError)` is only the status line, so "not authorised" and
        "throttled" — the two likeliest failures — read identically without this."""
        exc = self._http_error(
            403, {}, json.dumps({"__type": "com.amazon.coral.service#AccessDeniedException"}).encode()
        )
        summary = server.bedrock_error_summary(exc)
        assert "403" in summary
        assert "AccessDeniedException" in summary

    def test_error_code_from_header(self):
        exc = self._http_error(400, {"x-amzn-errortype": "ThrottlingException:http://internal/"}, b"")
        assert "ThrottlingException" in server.bedrock_error_summary(exc)

    def test_message_is_not_echoed_back(self):
        """An AccessDenied message names the role ARN the proxy signs with, and any
        caller the NetworkPolicy allows can read /run's response."""
        body = json.dumps(
            {
                "__type": "AccessDeniedException",
                "message": "User: arn:aws:sts::123456789012:assumed-role/sigv4-proxy/x is not authorized",
            }
        ).encode()
        summary = server.bedrock_error_summary(self._http_error(403, {}, body))
        assert "assumed-role" not in summary
        assert "AccessDeniedException" in summary

    def test_unparseable_body_falls_back_to_the_status_line(self):
        exc = self._http_error(500, {}, b"<html>gateway</html>")
        assert server.bedrock_error_summary(exc) == str(exc)


class TestRunTask:
    """Each stage's failure must be captured, never raised — callers need to see
    exactly which part of the credential path worked."""

    def test_clone_failure_stops_before_bedrock(self, monkeypatch):
        def boom(*a, **kw):
            raise subprocess.CalledProcessError(128, "git", stderr="could not read Username")

        monkeypatch.setattr(server, "clone_repo", boom)
        monkeypatch.setattr(server, "invoke_bedrock", lambda *a: pytest.fail("must not call Bedrock"))

        result = server.run_task({"repo": "org/private", "model": "m"})
        assert result["cloned"] is False
        assert "could not read Username" in result["errors"]["clone"]
        assert result["report"] == ""

    def test_missing_model_is_reported(self, monkeypatch):
        monkeypatch.setattr(server, "clone_repo", lambda *a, **kw: 7)
        monkeypatch.setattr(server, "DEFAULT_MODEL", "")

        result = server.run_task({"repo": "org/repo"})
        assert result["cloned"] is True
        assert result["file_count"] == 7
        assert result["errors"]["bedrock"].startswith("no model configured")

    def test_spec_model_overrides_the_default(self, monkeypatch):
        monkeypatch.setattr(server, "clone_repo", lambda *a, **kw: 1)
        monkeypatch.setattr(server, "top_level_entries", lambda dest: ["README.md"])
        monkeypatch.setattr(server, "invoke_bedrock", lambda model, prompt: f"used {model}")

        result = server.run_task({"repo": "org/repo", "model": "us.anthropic.override"})
        assert result["report"] == "used us.anthropic.override"
        assert result["top_level"] == ["README.md"]
        assert result["errors"] == {}

    def test_listing_failure_still_asks_bedrock(self, monkeypatch):
        """Grounding is best-effort: a repo we can't list is still worth asking about."""

        def boom(dest):
            raise subprocess.CalledProcessError(128, "git ls-tree", stderr="not a repository")

        monkeypatch.setattr(server, "clone_repo", lambda *a, **kw: 1)
        monkeypatch.setattr(server, "top_level_entries", boom)
        monkeypatch.setattr(server, "invoke_bedrock", lambda model, prompt: "report anyway")

        result = server.run_task({"repo": "org/repo", "model": "m"})
        assert result["report"] == "report anyway"
        assert result["top_level"] == []
        assert "not a repository" in result["errors"]["listing"]
        assert "bedrock" not in result["errors"]

    def test_bedrock_failure_is_captured(self, monkeypatch):
        def boom(model, prompt):
            raise urllib.error.URLError("sigv4-proxy unreachable")

        monkeypatch.setattr(server, "clone_repo", lambda *a, **kw: 1)
        monkeypatch.setattr(server, "top_level_entries", lambda dest: ["README.md"])
        monkeypatch.setattr(server, "invoke_bedrock", boom)

        result = server.run_task({"repo": "org/repo", "model": "m"})
        assert result["cloned"] is True
        assert "sigv4-proxy unreachable" in result["errors"]["bedrock"]

    @pytest.mark.parametrize(
        "exc",
        [
            http.client.IncompleteRead(b"11 bytes", 489),
            http.client.RemoteDisconnected("remote end closed connection"),
            TypeError("string indices must be integers"),
        ],
        ids=["truncated-body", "reset-status-line", "malformed-payload"],
    )
    def test_proxy_disconnect_mid_response_is_captured(self, monkeypatch, exc):
        """A proxy restart mid-response must still produce the `errors` object. An
        escaping exception closes the connection instead, and a closed connection
        cannot be told apart from the pod being gone."""

        def boom(model, prompt):
            raise exc

        monkeypatch.setattr(server, "clone_repo", lambda *a, **kw: 1)
        monkeypatch.setattr(server, "top_level_entries", lambda dest: ["README.md"])
        monkeypatch.setattr(server, "invoke_bedrock", boom)

        result = server.run_task({"repo": "org/repo", "model": "m"})
        assert result["cloned"] is True
        assert result["errors"]["bedrock"]

    def test_http_error_reports_the_aws_error_code(self, monkeypatch):
        def boom(model, prompt):
            raise urllib.error.HTTPError(
                "http://proxy/model/m/invoke",
                403,
                "Forbidden",
                {},
                io.BytesIO(json.dumps({"__type": "AccessDeniedException"}).encode()),
            )

        monkeypatch.setattr(server, "clone_repo", lambda *a, **kw: 1)
        monkeypatch.setattr(server, "top_level_entries", lambda dest: ["README.md"])
        monkeypatch.setattr(server, "invoke_bedrock", boom)

        result = server.run_task({"repo": "org/repo", "model": "m"})
        assert "AccessDeniedException" in result["errors"]["bedrock"]

    @pytest.mark.parametrize("ref", [None, 0, [], ""], ids=["null", "zero", "list", "empty"])
    def test_non_string_ref_falls_back_to_main(self, monkeypatch, ref):
        """`spec.get("ref", "main")` returns None for an explicit null, and None
        would reach git as a command argument."""
        seen = {}

        def spy_clone(repo, resolved_ref, dest):
            seen["ref"] = resolved_ref
            return 1

        monkeypatch.setattr(server, "clone_repo", spy_clone)
        monkeypatch.setattr(server, "top_level_entries", lambda dest: [])
        monkeypatch.setattr(server, "invoke_bedrock", lambda model, prompt: "ok")

        result = server.run_task({"repo": "org/repo", "ref": ref, "model": "m"})
        assert seen["ref"] == "main"
        assert result["errors"] == {}


@pytest.fixture
def worker():
    """The real HTTP worker on an ephemeral port (IPv6, as in the IPv6-only cluster)."""
    httpd = server.HTTPServerV6(("::1", 0), server.Handler)
    Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://[::1]:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


# Ignore any ambient HTTP(S)_PROXY — these requests go to a loopback socket.
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _get(url):
    return _opener.open(url, timeout=10)


def _post(url, payload):
    req = urllib.request.Request(  # noqa: S310  (loopback http:// built in-test)
        url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}
    )
    return json.loads(_opener.open(req, timeout=10).read())


class TestHTTPSurface:
    def test_healthz(self, worker):
        assert json.loads(_get(f"{worker}/healthz").read()) == {"status": "ok"}

    def test_unknown_path_is_404(self, worker):
        with pytest.raises(urllib.error.HTTPError) as exc:
            _get(f"{worker}/nope")
        assert exc.value.code == 404

    def test_run_requires_repo(self, worker):
        with pytest.raises(urllib.error.HTTPError) as exc:
            _post(f"{worker}/run", {"task": "no repo given"})
        assert exc.value.code == 400
        assert "missing 'repo'" in json.loads(exc.value.read())["error"]

    def test_post_to_unknown_path_is_404(self, worker):
        with pytest.raises(urllib.error.HTTPError) as exc:
            _post(f"{worker}/nope", {"repo": "org/repo"})
        assert exc.value.code == 404

    def test_run_returns_the_task_result(self, worker, monkeypatch):
        monkeypatch.setattr(server, "run_task", lambda spec: {"cloned": True, "report": f"ran {spec['repo']}"})
        assert _post(f"{worker}/run", {"repo": "org/repo"}) == {"cloned": True, "report": "ran org/repo"}

    @pytest.mark.parametrize("body", [[], None, 3, "text"], ids=["list", "null", "number", "string"])
    def test_non_object_body_is_400(self, worker, body):
        """These used to reach `spec.get("repo")` and raise AttributeError, so the
        caller got no response at all — indistinguishable from the pod being gone."""
        with pytest.raises(urllib.error.HTTPError) as exc:
            _post(f"{worker}/run", body)
        assert exc.value.code == 400
        assert "JSON object" in json.loads(exc.value.read())["error"]

    @pytest.mark.parametrize(
        ("field", "value"),
        [("ref", None), ("ref", 7), ("task", []), ("model", {})],
        ids=["ref-null", "ref-number", "task-list", "model-object"],
    )
    def test_wrong_field_type_is_400(self, worker, field, value):
        """Truth-testing would let 0 or [] through to a subprocess argument."""
        with pytest.raises(urllib.error.HTTPError) as exc:
            _post(f"{worker}/run", {"repo": "org/repo", field: value})
        assert exc.value.code == 400
        assert f"'{field}' must be a string" in json.loads(exc.value.read())["error"]


class TestRequestBodyLimits:
    """The body is read before the task lock, on a thread ThreadingHTTPServer does
    not cap, from a declared length the caller controls. /run is unauthenticated."""

    @staticmethod
    def _raw_post(worker, headers, body=b""):
        """Hand-rolled request: urllib sets Content-Length itself, and these cases
        are all about what a caller declares."""
        port = int(worker.rsplit(":", 1)[1])
        request = f"POST /run HTTP/1.1\r\nHost: [::1]:{port}\r\n"
        request += "".join(f"{k}: {v}\r\n" for k, v in headers.items())
        with socket.create_connection(("::1", port), timeout=10) as sock:
            sock.sendall(request.encode() + b"\r\n" + body)
            # The worker answers HTTP/1.0 and closes, so read to EOF — the body
            # arrives after the headers, in a later packet.
            received = b""
            while chunk := sock.recv(8192):
                received += chunk
        return received.decode(errors="replace")

    def test_non_numeric_content_length_is_400(self, worker):
        """This parse sat above the try, so a bad header raised ValueError uncaught
        and answered nothing at all."""
        response = self._raw_post(worker, {"Content-Length": "abc"})
        assert "400" in response.splitlines()[0]
        assert "invalid Content-Length" in response

    def test_negative_content_length_is_400(self, worker):
        """int() accepts -1 and read(-1) reads to end of file, so this parked a
        handler thread with no ceiling for as long as the caller stayed connected."""
        response = self._raw_post(worker, {"Content-Length": "-1"})
        assert "400" in response.splitlines()[0]

    def test_oversized_content_length_is_refused_before_reading(self, worker):
        """Rejected on the declared length: no body is sent here at all, so a
        response can only come from refusing before the read."""
        response = self._raw_post(worker, {"Content-Length": str(server.MAX_BODY_BYTES + 1)})
        assert "400" in response.splitlines()[0]

    def test_handler_has_a_socket_timeout(self, worker):
        """A stalled read must not outlive the timeout — otherwise a caller can hold
        an unbounded number of handler threads open in a 4 GiB pod."""
        assert 0 < server.Handler.timeout <= 60


class TestConcurrency:
    def test_healthz_answers_while_a_task_runs(self, worker, monkeypatch):
        """A single-threaded server starves /healthz during a clone, the readiness
        probe times out, and the pod is dropped from the Service mid-task —
        observed on the live cluster."""
        started, release = threading.Event(), threading.Event()

        def slow(spec):
            started.set()
            release.wait(10)
            return {"cloned": True}

        monkeypatch.setattr(server, "run_task", slow)
        task = Thread(target=lambda: _post(f"{worker}/run", {"repo": "org/repo"}), daemon=True)
        task.start()
        assert started.wait(5), "task never started"
        try:
            assert json.loads(_get(f"{worker}/healthz").read()) == {"status": "ok"}
        finally:
            release.set()
            task.join(10)

    def test_second_task_is_refused_not_queued(self, worker, monkeypatch):
        """One task at a time is still enforced — by the lock, not by blocking the
        listener, so the caller gets a clear 429 instead of hanging."""
        started, release = threading.Event(), threading.Event()

        def slow(spec):
            started.set()
            release.wait(10)
            return {"cloned": True}

        monkeypatch.setattr(server, "run_task", slow)
        first = Thread(target=lambda: _post(f"{worker}/run", {"repo": "org/repo"}), daemon=True)
        first.start()
        assert started.wait(5), "first task never started"
        try:
            with pytest.raises(urllib.error.HTTPError) as exc:
                _post(f"{worker}/run", {"repo": "org/other"})
            assert exc.value.code == 429
            assert "busy" in json.loads(exc.value.read())["error"]
        finally:
            release.set()
            first.join(10)


class TestMain:
    def test_binds_every_ipv6_interface(self, monkeypatch):
        """Regression guard: binding 0.0.0.0 (the HTTPServer default) leaves the
        worker unreachable on the IPv6-only cluster — the readiness probe and the
        Service both target the pod's IPv6 address."""
        bound = {}

        class FakeServer:
            def __init__(self, address, handler):
                bound["address"] = address
                bound["handler"] = handler

            def serve_forever(self):
                bound["served"] = True

        monkeypatch.setattr(server, "HTTPServerV6", FakeServer)
        server.main()

        assert bound["address"] == ("::", server.PORT)
        assert bound["handler"] is server.Handler
        assert bound["served"] is True

    def test_server_is_ipv6(self):
        import socket as _socket

        assert server.HTTPServerV6.address_family == _socket.AF_INET6
