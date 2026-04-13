"""Tests for the RPC server (src/k8s/rpc-server.py) — 100% coverage target."""

import json
import subprocess
import urllib.error
import urllib.request
from unittest.mock import MagicMock, mock_open, patch, call

import pytest


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _req(base_url, path, method="GET", body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{base_url}{path}", data=data, headers=headers or {}, method=method,
    )
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            more = resp.headers.get("X-More-Data")
            return resp.status, resp.read(), more
    except urllib.error.HTTPError as e:
        return e.code, e.read(), None


def _json(base_url, path, **kwargs):
    status, body, more = _req(base_url, path, **kwargs)
    return status, json.loads(body) if body else {}


# ---------------------------------------------------------------------------
# Unit tests: _check_auth
# ---------------------------------------------------------------------------

class TestCheckAuth:
    def test_no_token_set(self, rpc_mod):
        h = MagicMock()
        rpc_mod._auth_token = None
        assert rpc_mod._check_auth(h) is False
        h.send_error.assert_called_once_with(403, "No auth token set")

    def test_invalid_token(self, rpc_mod):
        h = MagicMock()
        h.headers.get.return_value = "wrong"
        rpc_mod._auth_token = "right"
        assert rpc_mod._check_auth(h) is False
        h.send_error.assert_called_once_with(403, "Invalid auth token")

    def test_valid_token(self, rpc_mod):
        h = MagicMock()
        h.headers.get.return_value = "tok"
        rpc_mod._auth_token = "tok"
        assert rpc_mod._check_auth(h) is True
        h.send_error.assert_not_called()


# ---------------------------------------------------------------------------
# Unit tests: _send_json / _read_body / _send_bytes
# ---------------------------------------------------------------------------

class TestSendJson:
    def test_default_status(self, rpc_mod):
        h = MagicMock()
        rpc_mod._send_json(h, {"k": "v"})
        h.send_response.assert_called_once_with(200)
        written = json.loads(h.wfile.write.call_args[0][0])
        assert written == {"k": "v"}

    def test_custom_status(self, rpc_mod):
        h = MagicMock()
        rpc_mod._send_json(h, {}, status=409)
        h.send_response.assert_called_once_with(409)


class TestReadBody:
    def test_no_content(self, rpc_mod):
        h = MagicMock()
        h.headers.get.return_value = 0
        assert rpc_mod._read_body(h) == {}

    def test_with_body(self, rpc_mod):
        h = MagicMock()
        payload = json.dumps({"id": "x"}).encode()
        h.headers.get.return_value = str(len(payload))
        h.rfile.read.return_value = payload
        assert rpc_mod._read_body(h) == {"id": "x"}


class TestSendBytes:
    def test_empty(self, rpc_mod):
        h = MagicMock()
        rpc_mod.Handler._send_bytes(h, b"")
        h.send_response.assert_called_once_with(200)
        h.wfile.write.assert_not_called()

    def test_with_data(self, rpc_mod):
        h = MagicMock()
        rpc_mod.Handler._send_bytes(h, b"abc")
        h.wfile.write.assert_called_once_with(b"abc")

    def test_more_data_header_true(self, rpc_mod):
        h = MagicMock()
        rpc_mod.Handler._send_bytes(h, b"abc", more_data=True)
        h.send_header.assert_any_call("X-More-Data", "true")

    def test_more_data_header_false(self, rpc_mod):
        h = MagicMock()
        rpc_mod.Handler._send_bytes(h, b"abc", more_data=False)
        h.send_header.assert_any_call("X-More-Data", "false")


# ---------------------------------------------------------------------------
# Unit tests: process management
# ---------------------------------------------------------------------------

class TestWaitForProcess:
    def test_success(self, rpc_mod):
        proc = MagicMock(returncode=0)
        rpc_mod._job_id = "j1"
        rpc_mod._wait_for_process(proc, "j1")
        assert rpc_mod._exit_code == 0
        assert rpc_mod._job_status == "completed"

    def test_failure(self, rpc_mod):
        proc = MagicMock(returncode=1)
        rpc_mod._job_id = "j1"
        rpc_mod._wait_for_process(proc, "j1")
        assert rpc_mod._exit_code == 1
        assert rpc_mod._job_status == "failed"

    def test_exception(self, rpc_mod):
        proc = MagicMock()
        proc.wait.side_effect = Exception("boom")
        rpc_mod._job_id = "j1"
        rpc_mod._wait_for_process(proc, "j1")
        assert rpc_mod._exit_code == -1
        assert rpc_mod._job_status == "failed"

    def test_wrong_job_id_no_update(self, rpc_mod):
        proc = MagicMock(returncode=0)
        rpc_mod._job_id = "other"
        rpc_mod._wait_for_process(proc, "j1")
        assert rpc_mod._exit_code is None
        assert rpc_mod._job_status == "idle"


class TestStartExec:
    def test_starts_process(self, rpc_mod, log_dir):
        with patch.object(rpc_mod.subprocess, "Popen") as mock_popen:
            mock_popen.return_value = MagicMock()
            proc, stdout_f, stderr_f = rpc_mod._start_exec("j1", "/tmp/s.sh")

            call_args = mock_popen.call_args
            assert call_args[0][0] == ["sh", "-e", "/tmp/s.sh"]
            assert call_args[1]["start_new_session"] is True
            assert (log_dir / "j1.stdout").exists()
            assert (log_dir / "j1.stderr").exists()
            stdout_f.close()
            stderr_f.close()

    def test_popen_failure_closes_files(self, rpc_mod, log_dir):
        with patch.object(
            rpc_mod.subprocess, "Popen", side_effect=OSError("boom")
        ):
            with pytest.raises(OSError, match="boom"):
                rpc_mod._start_exec("j1", "/tmp/s.sh")
            # Files should have been created then closed
            assert (log_dir / "j1.stdout").exists()
            assert (log_dir / "j1.stderr").exists()


class TestKillProcess:
    def test_no_process(self, rpc_mod):
        rpc_mod._process = None
        rpc_mod._kill_process()

    def test_already_exited(self, rpc_mod):
        proc = MagicMock()
        proc.poll.return_value = 0
        rpc_mod._process = proc
        rpc_mod._kill_process()

    def test_sigterm_succeeds(self, rpc_mod):
        proc = MagicMock(pid=42)
        proc.poll.return_value = None
        rpc_mod._process = proc
        with (
            patch.object(rpc_mod.os, "killpg"),
            patch.object(rpc_mod.os, "getpgid", return_value=42),
        ):
            rpc_mod._kill_process()
        assert rpc_mod._exit_code == -1
        assert rpc_mod._job_status == "failed"

    def test_sigterm_timeout_then_sigkill(self, rpc_mod):
        proc = MagicMock(pid=42)
        proc.poll.return_value = None
        proc.wait.side_effect = [subprocess.TimeoutExpired("c", 5), None]
        rpc_mod._process = proc
        with (
            patch.object(rpc_mod.os, "killpg") as mock_kill,
            patch.object(rpc_mod.os, "getpgid", return_value=42),
        ):
            rpc_mod._kill_process()
            assert mock_kill.call_count == 2

    def test_first_killpg_oserror(self, rpc_mod):
        proc = MagicMock(pid=42)
        proc.poll.return_value = None
        rpc_mod._process = proc
        with (
            patch.object(rpc_mod.os, "killpg", side_effect=OSError),
            patch.object(rpc_mod.os, "getpgid", return_value=42),
        ):
            rpc_mod._kill_process()
        assert rpc_mod._exit_code is None

    def test_sigkill_oserror(self, rpc_mod):
        proc = MagicMock(pid=42)
        proc.poll.return_value = None
        proc.wait.side_effect = [subprocess.TimeoutExpired("c", 5), None]
        rpc_mod._process = proc
        with (
            patch.object(rpc_mod.os, "killpg", side_effect=[None, OSError]),
            patch.object(rpc_mod.os, "getpgid", return_value=42),
        ):
            rpc_mod._kill_process()
        assert rpc_mod._exit_code == -1

    def test_exit_code_already_set(self, rpc_mod):
        proc = MagicMock(pid=42)
        proc.poll.return_value = None
        rpc_mod._process = proc
        rpc_mod._exit_code = 99
        with (
            patch.object(rpc_mod.os, "killpg"),
            patch.object(rpc_mod.os, "getpgid", return_value=42),
        ):
            rpc_mod._kill_process()
        assert rpc_mod._exit_code == 99


# ---------------------------------------------------------------------------
# Unit tests: helpers (_set_status, _set_globals)
# ---------------------------------------------------------------------------

class TestSetHelpers:
    def test_set_status(self, rpc_mod):
        rpc_mod._set_status("running")
        assert rpc_mod._job_status == "running"

    def test_set_globals(self, rpc_mod):
        proc = MagicMock()
        rpc_mod._set_globals("j1", "running", None, proc)
        assert rpc_mod._job_id == "j1"
        assert rpc_mod._job_status == "running"
        assert rpc_mod._exit_code is None
        assert rpc_mod._process is proc


# ---------------------------------------------------------------------------
# HTTP integration tests: endpoints
# ---------------------------------------------------------------------------

class TestHealthEndpoint:
    def test_returns_ok(self, server):
        status, data = _json(server, "/health")
        assert status == 200
        assert data == {"status": "ok"}


class TestExecEndpoint:
    def test_missing_token(self, server):
        status, _, _ = _req(server, "/exec", "POST", body={"id": "j", "path": "/s"})
        assert status == 403

    def test_missing_all_fields(self, server, rpc_mod):
        rpc_mod._auth_token = "tok"
        status, _, _ = _req(
            server, "/exec", "POST", body={}, headers={"X-Auth-Token": "tok"},
        )
        assert status == 400

    def test_missing_path_only(self, server, rpc_mod):
        rpc_mod._auth_token = "tok"
        status, _, _ = _req(
            server, "/exec", "POST",
            body={"id": "j"}, headers={"X-Auth-Token": "tok"},
        )
        assert status == 400

    def test_success(self, rpc_mod, server, log_dir):
        rpc_mod._auth_token = "tok"
        with patch.object(rpc_mod.subprocess, "Popen") as mock_popen:
            mock_popen.return_value = MagicMock()
            status, data = _json(
                server, "/exec", method="POST",
                body={"id": "j1", "path": "/s.sh"},
                headers={"X-Auth-Token": "tok"},
            )
        assert status == 200
        assert data["status"] == "running"

    def test_wrong_token(self, rpc_mod, server):
        rpc_mod._auth_token = "correct"
        status, _, _ = _req(
            server, "/exec", "POST",
            body={"id": "j", "path": "/s"},
            headers={"X-Auth-Token": "wrong"},
        )
        assert status == 403

    def test_job_already_running(self, rpc_mod, server):
        rpc_mod._auth_token = "tok"
        rpc_mod._job_status = "running"
        status, data = _json(
            server, "/exec", method="POST",
            body={"id": "j", "path": "/s"},
            headers={"X-Auth-Token": "tok"},
        )
        assert status == 409

    def test_job_starting_returns_409(self, rpc_mod, server):
        rpc_mod._auth_token = "tok"
        rpc_mod._job_status = "starting"
        status, data = _json(
            server, "/exec", method="POST",
            body={"id": "j", "path": "/s"},
            headers={"X-Auth-Token": "tok"},
        )
        assert status == 409

    def test_popen_failure_returns_500(self, rpc_mod, server, log_dir):
        rpc_mod._auth_token = "tok"
        with patch.object(
            rpc_mod.subprocess, "Popen", side_effect=OSError("spawn failed")
        ):
            status, data = _json(
                server, "/exec", method="POST",
                body={"id": "j1", "path": "/s.sh"},
                headers={"X-Auth-Token": "tok"},
            )
        assert status == 500
        assert "spawn failed" in data["error"]
        # Status should revert to idle (the previous state)
        assert rpc_mod._job_status == "idle"


class TestStatusEndpoint:
    def test_requires_auth(self, server):
        status, _, _ = _req(server, "/status")
        assert status == 403

    def test_returns_status(self, rpc_mod, server):
        rpc_mod._auth_token = "tok"
        rpc_mod._job_id = "j1"
        rpc_mod._job_status = "completed"
        rpc_mod._exit_code = 0
        status, data = _json(server, "/status", headers={"X-Auth-Token": "tok"})
        assert status == 200
        assert data == {"id": "j1", "status": "completed", "exit_code": 0}


class TestLogsEndpoint:
    def test_requires_auth(self, server):
        status, _, _ = _req(server, "/logs")
        assert status == 403

    def test_no_job(self, rpc_mod, server):
        rpc_mod._auth_token = "tok"
        status, body, _ = _req(server, "/logs", headers={"X-Auth-Token": "tok"})
        assert status == 200
        assert body == b""

    def test_stdout_default(self, rpc_mod, server, log_dir):
        rpc_mod._auth_token = "tok"
        rpc_mod._job_id = "j1"
        (log_dir / "j1.stdout").write_bytes(b"hello")
        status, body, more = _req(server, "/logs", headers={"X-Auth-Token": "tok"})
        assert status == 200
        assert body == b"hello"
        assert more == "false"

    def test_stderr_stream(self, rpc_mod, server, log_dir):
        rpc_mod._auth_token = "tok"
        rpc_mod._job_id = "j1"
        (log_dir / "j1.stderr").write_bytes(b"err output")
        status, body, _ = _req(
            server, "/logs?stream=stderr", headers={"X-Auth-Token": "tok"},
        )
        assert status == 200
        assert body == b"err output"

    def test_with_offset(self, rpc_mod, server, log_dir):
        rpc_mod._auth_token = "tok"
        rpc_mod._job_id = "j1"
        (log_dir / "j1.stdout").write_bytes(b"hello world")
        status, body, _ = _req(
            server, "/logs?offset=6", headers={"X-Auth-Token": "tok"},
        )
        assert status == 200
        assert body == b"world"

    def test_offset_beyond_eof(self, rpc_mod, server, log_dir):
        rpc_mod._auth_token = "tok"
        rpc_mod._job_id = "j1"
        (log_dir / "j1.stdout").write_bytes(b"hi")
        status, body, _ = _req(
            server, "/logs?offset=999", headers={"X-Auth-Token": "tok"},
        )
        assert status == 200
        assert body == b""

    def test_invalid_offset(self, rpc_mod, server, log_dir):
        rpc_mod._auth_token = "tok"
        rpc_mod._job_id = "j1"
        (log_dir / "j1.stdout").write_bytes(b"hello")
        status, body, _ = _req(
            server, "/logs?offset=abc", headers={"X-Auth-Token": "tok"},
        )
        assert status == 200
        assert body == b"hello"

    def test_multiple_query_params(self, rpc_mod, server, log_dir):
        rpc_mod._auth_token = "tok"
        rpc_mod._job_id = "j1"
        (log_dir / "j1.stdout").write_bytes(b"hello world")
        status, body, _ = _req(
            server, "/logs?foo=bar&offset=6", headers={"X-Auth-Token": "tok"},
        )
        assert status == 200
        assert body == b"world"

    def test_file_not_found(self, rpc_mod, server, log_dir):
        rpc_mod._auth_token = "tok"
        rpc_mod._job_id = "nonexistent"
        status, body, _ = _req(server, "/logs", headers={"X-Auth-Token": "tok"})
        assert status == 200
        assert body == b""

    def test_chunked_read_more_data(self, rpc_mod, server, log_dir):
        rpc_mod._auth_token = "tok"
        rpc_mod._job_id = "j1"
        # Write more than MAX_CHUNK (1 MB)
        big = b"A" * (rpc_mod.MAX_CHUNK + 100)
        (log_dir / "j1.stdout").write_bytes(big)
        status, body, more = _req(server, "/logs", headers={"X-Auth-Token": "tok"})
        assert status == 200
        assert len(body) == rpc_mod.MAX_CHUNK
        assert more == "true"

    def test_invalid_stream_defaults_stdout(self, rpc_mod, server, log_dir):
        rpc_mod._auth_token = "tok"
        rpc_mod._job_id = "j1"
        (log_dir / "j1.stdout").write_bytes(b"out")
        status, body, _ = _req(
            server, "/logs?stream=bogus", headers={"X-Auth-Token": "tok"},
        )
        assert status == 200
        assert body == b"out"

    def test_stream_and_offset(self, rpc_mod, server, log_dir):
        rpc_mod._auth_token = "tok"
        rpc_mod._job_id = "j1"
        (log_dir / "j1.stderr").write_bytes(b"error detail")
        status, body, _ = _req(
            server, "/logs?stream=stderr&offset=6",
            headers={"X-Auth-Token": "tok"},
        )
        assert status == 200
        assert body == b"detail"


class TestHeartbeatEndpoint:
    def test_requires_auth(self, server):
        status, _, _ = _req(server, "/heartbeat", "POST")
        assert status == 403

    def test_updates_heartbeat(self, rpc_mod, server):
        rpc_mod._auth_token = "tok"
        status, data = _json(
            server, "/heartbeat", method="POST",
            headers={"X-Auth-Token": "tok"},
        )
        assert status == 200
        assert data == {"status": "ok"}
        assert rpc_mod._last_heartbeat is not None


class TestUnknownEndpoints:
    def test_get_404(self, rpc_mod, server):
        rpc_mod._auth_token = "tok"
        status, _, _ = _req(server, "/nope", headers={"X-Auth-Token": "tok"})
        assert status == 404

    def test_post_404(self, rpc_mod, server):
        rpc_mod._auth_token = "tok"
        status, _, _ = _req(
            server, "/nope", "POST", headers={"X-Auth-Token": "tok"},
        )
        assert status == 404


# ---------------------------------------------------------------------------
# Unit tests: background / lifecycle
# ---------------------------------------------------------------------------

class TestHeartbeatWatchdog:
    def test_no_heartbeat_loops(self, rpc_mod):
        count = 0

        def fake_sleep(_):
            nonlocal count
            count += 1
            if count >= 3:
                raise RuntimeError("stop")

        with patch.object(rpc_mod.time, "sleep", side_effect=fake_sleep):
            with pytest.raises(RuntimeError, match="stop"):
                rpc_mod._heartbeat_watchdog()

    def test_within_timeout_continues(self, rpc_mod):
        rpc_mod._last_heartbeat = 95
        count = 0

        def fake_sleep(_):
            nonlocal count
            count += 1
            if count >= 2:
                raise RuntimeError("stop")

        with (
            patch.object(rpc_mod.time, "sleep", side_effect=fake_sleep),
            patch.object(rpc_mod.time, "time", return_value=100),
        ):
            with pytest.raises(RuntimeError, match="stop"):
                rpc_mod._heartbeat_watchdog()

    def test_timeout_kills_process_not_daemon(self, rpc_mod):
        """Watchdog kills the process but does NOT exit the daemon."""
        rpc_mod._last_heartbeat = 0
        call_count = 0

        def fake_sleep(_):
            nonlocal call_count
            call_count += 1
            # First iteration triggers the timeout; second proves we loop.
            if call_count >= 2:
                raise RuntimeError("stop")

        with (
            patch.object(rpc_mod.time, "sleep", side_effect=fake_sleep),
            patch.object(rpc_mod.time, "time", return_value=100),
            patch.object(rpc_mod, "_kill_process") as mock_kill,
        ):
            with pytest.raises(RuntimeError, match="stop"):
                rpc_mod._heartbeat_watchdog()
            mock_kill.assert_called_once()
            # _last_heartbeat should be reset to None after killing
            assert rpc_mod._last_heartbeat is None


class TestSignalHandler:
    def test_kills_and_exits(self, rpc_mod):
        with patch.object(rpc_mod, "_kill_process") as mock_kill:
            with pytest.raises(SystemExit):
                rpc_mod._signal_handler(None, None)
            mock_kill.assert_called_once()


class TestMain:
    def test_setup_and_serve(self, rpc_mod):
        with (
            patch.object(rpc_mod.sys, "argv", ["s", "--token", "secret"]),
            patch.object(rpc_mod.os, "makedirs") as mock_mkdirs,
            patch.object(rpc_mod.signal, "signal") as mock_sig,
            patch.object(rpc_mod.threading, "Thread") as mock_thr,
            patch.object(rpc_mod, "ThreadingHTTPServer") as mock_cls,
        ):
            mock_cls.return_value = MagicMock()
            mock_cls.return_value.serve_forever.side_effect = RuntimeError("x")
            with pytest.raises(RuntimeError):
                rpc_mod.main()
            mock_mkdirs.assert_called_once()
            assert mock_sig.call_count == 2
            mock_thr.return_value.start.assert_called_once()
            mock_cls.assert_called_once_with(("0.0.0.0", 8080), rpc_mod.Handler)
            assert rpc_mod._auth_token == "secret"

    def test_custom_port(self, rpc_mod):
        with (
            patch.object(rpc_mod.sys, "argv", ["s", "--port", "9999", "--token", "t"]),
            patch.object(rpc_mod.os, "makedirs"),
            patch.object(rpc_mod.signal, "signal"),
            patch.object(rpc_mod.threading, "Thread"),
            patch.object(rpc_mod, "ThreadingHTTPServer") as mock_cls,
        ):
            mock_cls.return_value = MagicMock()
            mock_cls.return_value.serve_forever.side_effect = RuntimeError("x")
            with pytest.raises(RuntimeError):
                rpc_mod.main()
            mock_cls.assert_called_once_with(("0.0.0.0", 9999), rpc_mod.Handler)

    def test_missing_token_exits(self, rpc_mod):
        with (
            patch.object(rpc_mod.sys, "argv", ["s"]),
        ):
            with pytest.raises(SystemExit):
                rpc_mod.main()


class TestLogRequest:
    def test_logs_non_heartbeat(self, rpc_mod, capsys):
        h = MagicMock(spec=rpc_mod.Handler)
        h.command = "GET"
        h.path = "/status"
        rpc_mod.Handler.log_request(h, code=200)
        captured = capsys.readouterr()
        assert "GET /status -> 200" in captured.out

    def test_skips_heartbeat(self, rpc_mod, capsys):
        h = MagicMock(spec=rpc_mod.Handler)
        h.command = "POST"
        h.path = "/heartbeat"
        rpc_mod.Handler.log_request(h, code=200)
        captured = capsys.readouterr()
        assert captured.out == ""

    def test_strips_query_string(self, rpc_mod, capsys):
        h = MagicMock(spec=rpc_mod.Handler)
        h.command = "GET"
        h.path = "/logs?offset=100"
        rpc_mod.Handler.log_request(h, code=200)
        captured = capsys.readouterr()
        assert "GET /logs -> 200" in captured.out
