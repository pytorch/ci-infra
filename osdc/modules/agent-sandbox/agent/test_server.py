"""Tests for the sandbox agent worker (server.py).

boto3 is installed in the agent image, not in this repo's venv, so it is stubbed
into sys.modules before importing the worker. Everything else runs for real:
`top_level_entries` against a real git repo, and the HTTP surface against a real
socket.
"""

import json
import subprocess
import sys
import types
import urllib.error
import urllib.request
from threading import Thread

import pytest


def _install_boto3_stub() -> None:
    """Minimal stand-ins for the two AWS imports server.py makes at module scope."""
    if "boto3" not in sys.modules:
        boto3 = types.ModuleType("boto3")
        boto3.client = lambda *a, **kw: None  # replaced per-test via monkeypatch
        sys.modules["boto3"] = boto3
    if "botocore.exceptions" not in sys.modules:
        botocore = sys.modules.setdefault("botocore", types.ModuleType("botocore"))
        exceptions = types.ModuleType("botocore.exceptions")

        class BotoCoreError(Exception):
            pass

        class ClientError(Exception):
            pass

        exceptions.BotoCoreError = BotoCoreError
        exceptions.ClientError = ClientError
        botocore.exceptions = exceptions
        sys.modules["botocore.exceptions"] = exceptions


_install_boto3_stub()

import server  # noqa: E402  (must follow the boto3 stub)


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


class TestInvokeBedrock:
    def test_sends_the_messages_api_body_and_returns_the_text(self, monkeypatch):
        captured = {}

        class FakeClient:
            def invoke_model(self, **kwargs):
                captured.update(kwargs)
                payload = json.dumps({"content": [{"type": "text", "text": "the report"}]})
                return {"body": types.SimpleNamespace(read=lambda: payload.encode())}

        monkeypatch.setattr(server.boto3, "client", lambda *a, **kw: FakeClient(), raising=False)
        assert server.invoke_bedrock("us.anthropic.claude-haiku-4-5-20251001-v1:0", "hello") == "the report"

        assert captured["modelId"] == "us.anthropic.claude-haiku-4-5-20251001-v1:0"
        body = json.loads(captured["body"])
        assert body["anthropic_version"] == "bedrock-2023-05-31"
        assert body["messages"] == [{"role": "user", "content": [{"type": "text", "text": "hello"}]}]

    def test_empty_content_yields_empty_report(self, monkeypatch):
        class FakeClient:
            def invoke_model(self, **kwargs):
                return {"body": types.SimpleNamespace(read=lambda: b'{"content": []}')}

        monkeypatch.setattr(server.boto3, "client", lambda *a, **kw: FakeClient(), raising=False)
        assert server.invoke_bedrock("m", "hello") == ""


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
            raise server.ClientError("AccessDeniedException: not authorized")

        monkeypatch.setattr(server, "clone_repo", lambda *a, **kw: 1)
        monkeypatch.setattr(server, "top_level_entries", lambda dest: ["README.md"])
        monkeypatch.setattr(server, "invoke_bedrock", boom)

        result = server.run_task({"repo": "org/repo", "model": "m"})
        assert result["cloned"] is True
        assert "AccessDeniedException" in result["errors"]["bedrock"]


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
