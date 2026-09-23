"""The effect applier, against a fake GitHub API on loopback."""

from __future__ import annotations

import json
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

import apply_effects
import pytest

HEAD = "a" * 40
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


@pytest.fixture
def github():
    state = {"head": HEAD, "calls": [], "fail": None}

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, payload):
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            state["calls"].append(("GET", self.path, self.headers.get("Authorization"), None))
            self._send(200, {"head": {"sha": state["head"]}})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["calls"].append(("POST", self.path, self.headers.get("Authorization"), body))
            if state["fail"]:
                self._send(state["fail"], {"message": "nope"})
            else:
                self._send(201, {"html_url": f"https://github.test{self.path}/1"})

        def log_message(self, *args):
            pass

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    state["api"] = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield state
    httpd.shutdown()
    httpd.server_close()


def comment(**overrides):
    return {"effect": "pr_comment", "repo": "pytorch/pytorch", "head_sha": HEAD, "body": "Looks good.", **overrides}


def check(**overrides):
    return {
        "effect": "check_run",
        "repo": "pytorch/pytorch",
        "head_sha": HEAD,
        "body": "Summary",
        "name": "ai-review",
        "conclusion": "neutral",
        "title": "AI review",
        **overrides,
    }


def run(github, tmp_path, effects, **env):
    result = tmp_path / "result.json"
    result.write_text(json.dumps({"effects": effects}))
    base = {
        "INPUT_RESULT_FILE": str(result),
        "INPUT_PR_NUMBER": "42",
        "INPUT_REPOSITORY": "pytorch/pytorch",
        "INPUT_GITHUB_TOKEN": "ghs_x",
        "GITHUB_API_URL": github["api"],
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md"),
    }
    return apply_effects.main({**base, **env}, opener=OPENER)


def test_a_comment_is_a_review_bound_to_the_reviewed_commit(github, tmp_path):
    assert run(github, tmp_path, [comment()]) == 0
    method, path, auth, body = github["calls"][-1]
    assert (method, path, auth) == ("POST", "/repos/pytorch/pytorch/pulls/42/reviews", "Bearer ghs_x")
    assert body == {"commit_id": HEAD, "event": "COMMENT", "body": "Looks good."}
    assert "posted pr_comment" in (tmp_path / "summary.md").read_text()


def test_a_check_run_is_created_on_the_reviewed_commit(github, tmp_path):
    assert run(github, tmp_path, [check()]) == 0
    _, path, _, body = github["calls"][-1]
    assert path == "/repos/pytorch/pytorch/check-runs"
    assert body["head_sha"] == HEAD
    assert (body["name"], body["conclusion"], body["status"]) == ("ai-review", "neutral", "completed")


def test_nothing_is_written_when_the_pr_moved(github, tmp_path, capsys):
    github["head"] = "b" * 40
    assert run(github, tmp_path, [comment()]) == 0
    assert [c[0] for c in github["calls"]] == ["GET"]
    assert "moved from" in capsys.readouterr().out


@pytest.mark.parametrize(
    "effect",
    [
        comment(repo="attacker/repo"),
        comment(head_sha="main"),
        comment(body=" "),
        {"effect": "merge", "repo": "pytorch/pytorch", "head_sha": HEAD, "body": "x"},
        check(conclusion=None),
        "not an object",
    ],
    ids=["other-repo", "no-sha", "empty-body", "unknown-kind", "check-missing-field", "not-object"],
)
def test_a_malformed_effect_fails_the_step_without_writing(github, tmp_path, effect):
    assert run(github, tmp_path, [effect]) == 1
    assert not [c for c in github["calls"] if c[0] == "POST"]


def test_an_api_failure_fails_the_step(github, tmp_path, capsys):
    github["fail"] = 403
    assert run(github, tmp_path, [comment()]) == 1
    assert "HTTP 403" in capsys.readouterr().out


def test_no_effects_is_a_quiet_success(github, tmp_path):
    assert run(github, tmp_path, []) == 0
    assert github["calls"] == []


@pytest.mark.parametrize(
    "env",
    [{"INPUT_PR_NUMBER": ""}, {"INPUT_PR_NUMBER": "-1"}, {"INPUT_REPOSITORY": ""}, {"INPUT_GITHUB_TOKEN": ""}],
)
def test_missing_inputs_are_an_error(github, tmp_path, env):
    assert run(github, tmp_path, [comment()], **env) == 1


def test_the_job_token_is_refused_for_another_repository(github, tmp_path, capsys):
    env = {"INPUT_TOKEN_IS_JOB_TOKEN": "true", "GITHUB_REPOSITORY": "pytorch/ciforge"}
    assert run(github, tmp_path, [comment()], **env) == 1
    assert github["calls"] == []
    assert "can write only to pytorch/ciforge" in capsys.readouterr().out


@pytest.mark.parametrize(
    "env",
    [
        {"INPUT_TOKEN_IS_JOB_TOKEN": "true", "GITHUB_REPOSITORY": "PyTorch/PyTorch"},
        {"INPUT_TOKEN_IS_JOB_TOKEN": "false", "GITHUB_REPOSITORY": "pytorch/ciforge"},
    ],
    ids=["job-token-own-repo", "target-token-other-repo"],
)
def test_a_token_that_can_write_to_the_target_is_used(github, tmp_path, env):
    assert run(github, tmp_path, [comment()], **env) == 0
    assert github["calls"][-1][0] == "POST"


def test_an_unreadable_result_is_an_error(github, tmp_path):
    assert run(github, tmp_path, [comment()], INPUT_RESULT_FILE=str(tmp_path / "absent.json")) == 1


def test_a_record_cannot_start_a_workflow_command(github, tmp_path, capsys):
    run(github, tmp_path, [comment(repo="x\n::add-mask::y")])
    assert not any(line.startswith("::add-mask::y") for line in capsys.readouterr().out.splitlines())
