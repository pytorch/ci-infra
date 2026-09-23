"""The GitHub Action client, against a fake token service and dispatcher on loopback."""

from __future__ import annotations

import json
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
import sandbox_client
from sandbox_client import ClientError


@pytest.fixture
def fake():
    """One loopback server playing both GitHub's token endpoint and the dispatcher."""
    state = {
        "run": [(200, {"task_id": "0123456789ab", "report": "looks fine", "errors": {}})],
        "requests": [],
        "token": "tok",
    }

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, payload, raw=None):
            body = raw if raw is not None else json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path.startswith("/token"):
                state["requests"].append(("token", self.path, self.headers.get("Authorization")))
                self._send(200, {"value": state["token"]})
            elif self.path == "/healthz":
                self._send(200, {"status": "ok", "in_flight": 6, "capacity": 6})
            else:
                self._send(404, {})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["requests"].append(("run", body, self.headers.get("Authorization")))
            code, payload = state["run"].pop(0) if len(state["run"]) > 1 else state["run"][0]
            if payload is None:
                self._send(code, None, raw=b"not json")
            elif isinstance(payload, bytes):
                self._send(code, None, raw=payload)
            else:
                self._send(code, payload)

        def log_message(self, *args):
            pass

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    state["base"] = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield state
    httpd.shutdown()
    httpd.server_close()


def env_for(fake, tmp_path, **inputs):
    env = {
        "ACTIONS_ID_TOKEN_REQUEST_URL": f"{fake['base']}/token?api-version=2.0",
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "runtime-bearer",
        "INPUT_MANIFEST": "ciforge-pr-review",
        "INPUT_TASK": "Review this change.",
        "INPUT_ENDPOINT": fake["base"],
        "INPUT_MAX_WAIT": "30",
        "RUNNER_TEMP": str(tmp_path),
        "GITHUB_OUTPUT": str(tmp_path / "out"),
    }
    env.update({f"INPUT_{k.upper()}": v for k, v in inputs.items()})
    return env


OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def run(env):
    return sandbox_client.main(env, opener=OPENER, token_opener=OPENER)


def outputs(tmp_path) -> dict:
    lines = (tmp_path / "out").read_text().splitlines()
    parsed, i = {}, 0
    while i < len(lines):
        name, delimiter = lines[i].split("<<")
        end = lines.index(delimiter, i + 1)
        parsed[name] = "\n".join(lines[i + 1 : end])
        i = end + 1
    return parsed


def test_a_finished_task_writes_its_outputs(fake, tmp_path, capsys):
    assert run(env_for(fake, tmp_path, repo="pytorch/test-infra", ref="main", base="a" * 40)) == 0
    out = outputs(tmp_path)
    assert out["task-id"] == "0123456789ab"
    assert out["report"] == "looks fine"
    assert json.loads(Path(out["result-file"]).read_text())["report"] == "looks fine"
    kind, body, auth = fake["requests"][-1]
    assert kind == "run"
    assert body == {
        "manifest": "ciforge-pr-review",
        "task": "Review this change.",
        "wait": True,
        "repo": "pytorch/test-infra",
        "ref": "main",
        "base": "a" * 40,
    }
    assert auth == "Bearer tok"
    assert "::add-mask::tok" in capsys.readouterr().out


def test_the_token_is_minted_for_the_dispatcher_audience(fake, tmp_path):
    run(env_for(fake, tmp_path))
    kind, path, auth = fake["requests"][0]
    assert kind == "token"
    assert path == "/token?api-version=2.0&audience=agent-service"
    assert auth == "bearer runtime-bearer"


def test_a_token_url_without_a_query_gets_one(fake, tmp_path):
    env = env_for(fake, tmp_path)
    env["ACTIONS_ID_TOKEN_REQUEST_URL"] = f"{fake['base']}/token"
    run(env)
    assert fake["requests"][0][1] == "/token?audience=agent-service"


def test_an_accepted_task_returns_the_id_without_a_report(fake, tmp_path):
    fake["run"] = [(202, {"task_id": "0123456789ab", "state": "running"})]
    assert run(env_for(fake, tmp_path, wait="false")) == 0
    out = outputs(tmp_path)
    assert out["task-id"] == "0123456789ab"
    assert "report" not in out
    assert fake["requests"][-1][1]["wait"] is False


def test_a_report_cannot_inject_outputs(fake, tmp_path):
    fake["run"] = [(200, {"task_id": "0123456789ab", "report": "x\nEOF\ninjected<<EOF\nboom\nEOF", "errors": {}})]
    assert run(env_for(fake, tmp_path)) == 0
    out = outputs(tmp_path)
    assert set(out) == {"task-id", "result-file", "report"}
    assert "injected<<EOF" in out["report"]


def test_task_controlled_text_cannot_start_a_workflow_command(fake, tmp_path, capsys):
    fake["run"] = [(200, {"task_id": "x\n::stop-commands::t", "report": "", "errors": {"e": "a\n::add-mask::b"}})]
    assert run(env_for(fake, tmp_path)) == 1
    lines = capsys.readouterr().out.splitlines()
    assert not any(line.startswith(("::stop-commands", "::add-mask::b")) for line in lines)


@pytest.mark.parametrize("task_id", ["x\n::stop-commands::t", "0123456789ab\n", "0123456789AB", 12345, None], ids=repr)
def test_a_malformed_task_id_fails_the_step_and_never_reaches_an_output(fake, tmp_path, capsys, task_id):
    fake["run"] = [(200, {"task_id": task_id, "report": "ok", "errors": {}})]
    assert run(env_for(fake, tmp_path)) == 1
    assert outputs(tmp_path)["task-id"] == ""
    assert "no well-formed task id" in capsys.readouterr().out


@pytest.mark.parametrize("code", [200, 202])
@pytest.mark.parametrize("body", [b"", b"[]", b"null", b'"ok"'], ids=repr)
def test_a_success_without_a_json_object_fails_the_step(fake, tmp_path, capsys, code, body):
    fake["run"] = [(code, body)]
    assert run(env_for(fake, tmp_path)) == 1
    assert "without a JSON object body" in capsys.readouterr().out
    assert not (tmp_path / "out").exists()
    assert [r[0] for r in fake["requests"]].count("run") == 1  # not retried


def test_an_error_reason_cannot_start_a_workflow_command(fake, tmp_path, capsys):
    fake["run"] = [(403, {"error": "no\n::add-mask::secret"})]
    assert run(env_for(fake, tmp_path)) == 1
    assert "%0A::add-mask::secret" in capsys.readouterr().out


def test_two_invocations_keep_two_result_files(fake, tmp_path):
    run(env_for(fake, tmp_path))
    first = outputs(tmp_path)["result-file"]
    (tmp_path / "out").unlink()
    run(env_for(fake, tmp_path))
    assert outputs(tmp_path)["result-file"] != first


def test_429_is_retried_then_succeeds(fake, tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox_client.time, "sleep", lambda s: None)
    fake["run"] = [(429, {"error": "at capacity"}), (200, {"task_id": "0123456789ab", "report": "ok", "errors": {}})]
    assert run(env_for(fake, tmp_path)) == 0
    assert [r[0] for r in fake["requests"]].count("run") == 2
    assert [r[0] for r in fake["requests"]].count("token") == 2, "a fresh token per attempt"


def test_429_gives_up_when_the_budget_is_spent(fake, tmp_path, capsys):
    fake["run"] = [(429, {"error": "at capacity"})]
    assert run(env_for(fake, tmp_path, max_wait="0")) == 1
    err = capsys.readouterr().out
    assert "HTTP 429" in err
    assert "in_flight" in err, "the failure carries a /healthz reading"


@pytest.mark.parametrize("clock_values", [[0.0, 1.0, 30.0]], ids=["expired-while-sleeping-or-minting"])
def test_no_retry_is_sent_after_the_budget_expires(clock_values):
    """Sleeping out the remainder (or waiting on a slow token mint) and then posting
    anyway would admit a task past the budget the caller set."""
    clock = iter(clock_values).__next__
    posts = []

    class Opener:
        def open(self, request, timeout):
            posts.append(request.full_url)
            raise sandbox_client.urllib.error.HTTPError(request.full_url, 429, "busy", {}, None)

    status, _ = sandbox_client.call_run(
        Opener(), _TokenOpener(), _TOKEN_ENV, "http://d", {}, 30, sleep=lambda s: None, clock=clock
    )
    assert status == 429
    assert len(posts) == 1


class _TokenOpener:
    def open(self, request, timeout):
        class R:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self, n):
                return b'{"value": "t"}'

        return R()


_TOKEN_ENV = {"ACTIONS_ID_TOKEN_REQUEST_URL": "http://t/x", "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "b"}


def test_the_backoff_is_bounded_by_the_budget():
    clock = iter([0.0, 1.0, 2.0, 29.0, 29.5, 31.0]).__next__  # start, 429, re-mint, 429, re-mint, 429
    slept = []

    class Opener:
        def open(self, request, timeout):
            raise sandbox_client.urllib.error.HTTPError(request.full_url, 429, "busy", {}, None)

    env = {"ACTIONS_ID_TOKEN_REQUEST_URL": "http://t/x", "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "b"}

    class TokenOpener:
        def open(self, request, timeout):
            class R:
                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

                def read(self, n):
                    return b'{"value": "t"}'

            return R()

    status, _ = sandbox_client.call_run(
        Opener(), TokenOpener(), env, "http://d", {}, 30, sleep=slept.append, clock=clock
    )
    assert status == 429
    assert all(s <= 30 for s in slept)
    assert slept[-1] <= 1.0, "the last wait is capped by what is left of the budget"


@pytest.mark.parametrize("code", [400, 401, 403, 500])
def test_other_errors_are_not_retried(fake, tmp_path, code, capsys):
    fake["run"] = [(code, {"error": "nope"})]
    assert run(env_for(fake, tmp_path)) == 1
    assert [r[0] for r in fake["requests"]].count("run") == 1
    assert f"HTTP {code}: nope" in capsys.readouterr().out


def test_a_task_that_reported_errors_fails_the_step_but_keeps_its_outputs(fake, tmp_path):
    fake["run"] = [(200, {"task_id": "0123456789ab", "report": "", "errors": {"clone": "boom"}})]
    assert run(env_for(fake, tmp_path)) == 1
    assert outputs(tmp_path)["task-id"] == "0123456789ab"


def test_a_transport_failure_is_not_retried(tmp_path, fake, capsys):
    env = env_for(fake, tmp_path)
    env["INPUT_ENDPOINT"] = "http://127.0.0.1:1"
    assert run(env) == 1
    assert "could not complete /run" in capsys.readouterr().out


def test_a_non_json_success_body_fails_cleanly(fake, tmp_path, capsys):
    fake["run"] = [(200, None)]
    assert run(env_for(fake, tmp_path)) == 1
    assert "::error::" in capsys.readouterr().out


def test_a_non_json_error_body_still_reports_the_status(fake, tmp_path, capsys):
    fake["run"] = [(502, None)]
    assert run(env_for(fake, tmp_path)) == 1
    assert "HTTP 502: no reason given" in capsys.readouterr().out


def test_without_id_token_permission_the_step_says_why(fake, tmp_path, capsys):
    env = env_for(fake, tmp_path)
    del env["ACTIONS_ID_TOKEN_REQUEST_TOKEN"]
    assert run(env) == 1
    assert "id-token: write" in capsys.readouterr().out


def test_an_unreachable_token_service_is_a_clear_error(fake, tmp_path, capsys):
    env = env_for(fake, tmp_path)
    env["ACTIONS_ID_TOKEN_REQUEST_URL"] = "http://127.0.0.1:1/token"  # noqa: S105  (a URL, not a secret)
    assert run(env) == 1
    assert "could not mint an OIDC token" in capsys.readouterr().out


def test_an_empty_token_is_refused(fake, tmp_path, capsys):
    fake["token"] = ""
    assert run(env_for(fake, tmp_path)) == 1
    assert "returned no token" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("inputs", "message"),
    [({"manifest": ""}, "`manifest` is required"), ({"task": "  "}, "`task` is required"), ({"wait": "yes"}, "`wait`")],
)
def test_bad_inputs_fail_before_any_request(fake, tmp_path, inputs, message, capsys):
    with pytest.raises(ClientError, match=message):
        sandbox_client.build_body(env_for(fake, tmp_path, **inputs))
    assert run(env_for(fake, tmp_path, **inputs)) == 1
    assert fake["requests"] == []


@pytest.mark.parametrize("value", ["soon", "nan", "inf", "1e309", "-5"])
def test_a_bad_max_wait_is_an_error_before_any_request(fake, tmp_path, value):
    assert run(env_for(fake, tmp_path, max_wait=value)) == 1
    assert fake["requests"] == []


def test_healthz_failure_is_reported_not_raised():
    assert sandbox_client.healthz(OPENER, "http://127.0.0.1:1").startswith("unavailable")


def test_without_github_output_the_step_still_succeeds(fake, tmp_path):
    env = env_for(fake, tmp_path)
    del env["GITHUB_OUTPUT"]
    assert run(env) == 0


def test_the_action_passes_inputs_as_env_not_as_script_text():
    """A task string is caller data; interpolating it into `run:` would make it shell."""
    import yaml

    action = yaml.safe_load((Path(__file__).parent / "action.yml").read_text())
    step = action["runs"]["steps"][0]
    assert "${{" not in step["run"]
    for name in action["inputs"]:
        assert f"INPUT_{name.upper().replace('-', '_')}" in step["env"]
