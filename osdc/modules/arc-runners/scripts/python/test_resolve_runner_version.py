"""Tests for resolve_runner_version.py."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests
import resolve_runner_version as rrv
from lightkube.core.exceptions import ApiError

_SHA_A = "a" * 40
_SHA_B = "b" * 40
_SHA_C = "c" * 40


def _api_error(code: int) -> ApiError:
    err = ApiError.__new__(ApiError)
    err.status = MagicMock(code=code)
    return err


def _release_response(tag: str = "v2.335.0") -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {"tag_name": tag}
    return resp


def _configmap(history: list[dict[str, str]] | str, resource_version: str | None = "100") -> MagicMock:
    cm = MagicMock()
    cm.data = {rrv.CM_KEY: history if isinstance(history, str) else json.dumps(history)}
    cm.metadata = MagicMock(resourceVersion=resource_version)
    return cm


def _crane_result(digest: str = "sha256:abc123") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["crane", "digest", "x"], returncode=0, stdout=f"{digest}\n", stderr="")


def _entry(sha: str, tag: str, digest: str, resolved_at: str = "2026-06-01T00:00:00Z") -> dict[str, str]:
    return {"osdc_sha": sha, "tag": tag, "digest": digest, "resolved_at": resolved_at}


@pytest.fixture
def fixed_now():
    return datetime(2026, 6, 15, 17, 42, 11, tzinfo=UTC)


@pytest.fixture
def patched(monkeypatch, fixed_now):
    monkeypatch.setattr(rrv, "now_utc", lambda: fixed_now)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("OSDC_RESOLVER_READONLY", raising=False)
    return monkeypatch


# --- osdc_root ---


class TestOsdcRoot:
    def test_env_var_present(self, monkeypatch):
        monkeypatch.setenv("OSDC_UPSTREAM", "/custom/path")
        assert rrv.osdc_root() == "/custom/path"

    def test_env_var_absent_falls_back_to_script_parent(self, monkeypatch):
        monkeypatch.delenv("OSDC_UPSTREAM", raising=False)
        expected = str(Path(rrv.__file__).resolve().parents[3])
        assert rrv.osdc_root() == expected

    def test_env_var_empty_falls_back(self, monkeypatch):
        monkeypatch.setenv("OSDC_UPSTREAM", "")
        expected = str(Path(rrv.__file__).resolve().parents[3])
        assert rrv.osdc_root() == expected


# --- osdc_sha ---


class TestOsdcSha:
    def test_returns_stripped_sha(self, monkeypatch):
        monkeypatch.setenv("OSDC_UPSTREAM", "/fake/root")
        captured = {}

        def fake_run(cmd, check, capture_output, text):
            captured["cmd"] = cmd
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=f"{_SHA_A}\n", stderr="")

        monkeypatch.setattr(rrv.subprocess, "run", fake_run)
        assert rrv.osdc_sha() == _SHA_A
        assert captured["cmd"] == ["git", "-C", "/fake/root", "log", "-1", "--format=%H", "--", rrv.OSDC_PATH]
        assert rrv.OSDC_PATH == "."

    def test_empty_output_raises(self, monkeypatch):
        monkeypatch.setenv("OSDC_UPSTREAM", "/fake/root")
        monkeypatch.setattr(
            rrv.subprocess,
            "run",
            lambda *a, **kw: subprocess.CompletedProcess(args=a[0], returncode=0, stdout="\n", stderr=""),
        )
        with pytest.raises(ValueError, match="no commit history under"):
            rrv.osdc_sha()

    def test_uses_osdc_root_fallback_when_env_absent(self, monkeypatch):
        monkeypatch.delenv("OSDC_UPSTREAM", raising=False)
        captured = {}

        def fake_run(cmd, check, capture_output, text):
            captured["cmd"] = cmd
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=f"{_SHA_A}\n", stderr="")

        monkeypatch.setattr(rrv.subprocess, "run", fake_run)
        rrv.osdc_sha()
        expected_root = str(Path(rrv.__file__).resolve().parents[3])
        assert captured["cmd"][2] == expected_root

    def test_subprocess_error_propagates(self, monkeypatch):
        monkeypatch.setenv("OSDC_UPSTREAM", "/fake/root")

        def fake_run(*a, **kw):
            raise subprocess.CalledProcessError(returncode=128, cmd=a[0], stderr="not a git repo")

        monkeypatch.setattr(rrv.subprocess, "run", fake_run)
        with pytest.raises(subprocess.CalledProcessError):
            rrv.osdc_sha()


# --- fetch_latest_tag ---


class TestFetchLatestTag:
    def test_no_token_no_auth_header(self, patched):
        captured = {}

        def fake_get(url, headers, timeout):
            captured["url"] = url
            captured["headers"] = headers
            captured["timeout"] = timeout
            return _release_response("v2.335.0")

        patched.setattr(rrv.requests, "get", fake_get)
        tag = rrv.fetch_latest_tag(None)
        assert tag == "2.335.0"
        assert "Authorization" not in captured["headers"]
        assert captured["url"] == rrv.GITHUB_RELEASES_URL
        assert captured["timeout"] == rrv.REQUEST_TIMEOUT_SECONDS

    def test_token_present_adds_bearer_header(self, patched):
        captured = {}

        def fake_get(url, headers, timeout):
            captured["headers"] = headers
            return _release_response("2.335.0")

        patched.setattr(rrv.requests, "get", fake_get)
        tag = rrv.fetch_latest_tag("ghp_xyz")
        assert tag == "2.335.0"
        assert captured["headers"]["Authorization"] == "Bearer ghp_xyz"

    def test_missing_tag_name_raises(self, patched):
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {"name": "no-tag"}
        patched.setattr(rrv.requests, "get", lambda *a, **kw: resp)
        with pytest.raises(ValueError, match="tag_name"):
            rrv.fetch_latest_tag(None)

    def test_empty_tag_name_raises(self, patched):
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {"tag_name": ""}
        patched.setattr(rrv.requests, "get", lambda *a, **kw: resp)
        with pytest.raises(ValueError, match="tag_name"):
            rrv.fetch_latest_tag(None)


# --- read_history ---


class TestReadHistory:
    def test_404_returns_empty_and_false_and_none_rv(self):
        client = MagicMock()
        client.get.side_effect = _api_error(404)
        history, exists, rv = rrv.read_history(client)
        assert history == []
        assert exists is False
        assert rv is None

    def test_non_404_api_error_reraised(self):
        client = MagicMock()
        client.get.side_effect = _api_error(403)
        with pytest.raises(ApiError):
            rrv.read_history(client)

    def test_parses_valid_history_and_returns_resource_version(self):
        entries = [_entry(_SHA_A, "2.335.0", "sha256:aaa", "2026-06-15T17:42:11Z")]
        client = MagicMock()
        client.get.return_value = _configmap(entries, resource_version="12345")
        history, exists, rv = rrv.read_history(client)
        assert history == entries
        assert exists is True
        assert rv == "12345"

    def test_missing_data_key_returns_empty_exists(self):
        client = MagicMock()
        cm = MagicMock()
        cm.data = {}
        cm.metadata = MagicMock(resourceVersion="77")
        client.get.return_value = cm
        history, exists, rv = rrv.read_history(client)
        assert history == []
        assert exists is True
        assert rv == "77"

    def test_none_data_returns_empty_exists(self):
        client = MagicMock()
        cm = MagicMock()
        cm.data = None
        cm.metadata = MagicMock(resourceVersion="78")
        client.get.return_value = cm
        history, exists, rv = rrv.read_history(client)
        assert history == []
        assert exists is True
        assert rv == "78"

    def test_none_metadata_returns_none_rv(self):
        client = MagicMock()
        cm = MagicMock()
        cm.data = None
        cm.metadata = None
        client.get.return_value = cm
        history, exists, rv = rrv.read_history(client)
        assert history == []
        assert exists is True
        assert rv is None

    def test_malformed_json_raises(self):
        client = MagicMock()
        client.get.return_value = _configmap("not-json{")
        with pytest.raises(json.JSONDecodeError):
            rrv.read_history(client)

    def test_non_list_json_raises(self):
        client = MagicMock()
        client.get.return_value = _configmap("{}")
        with pytest.raises(ValueError, match="must contain a JSON list"):
            rrv.read_history(client)

    def test_entry_missing_osdc_sha_raises(self):
        client = MagicMock()
        client.get.return_value = _configmap([{"tag": "2.0", "digest": "sha256:x"}])
        with pytest.raises(ValueError, match="osdc_sha"):
            rrv.read_history(client)

    def test_entry_missing_tag_raises(self):
        client = MagicMock()
        client.get.return_value = _configmap([{"osdc_sha": _SHA_A, "digest": "sha256:x"}])
        with pytest.raises(ValueError, match="osdc_sha"):
            rrv.read_history(client)

    def test_entry_missing_digest_raises(self):
        client = MagicMock()
        client.get.return_value = _configmap([{"osdc_sha": _SHA_A, "tag": "2.0"}])
        with pytest.raises(ValueError, match="osdc_sha"):
            rrv.read_history(client)

    def test_entry_not_object_raises(self):
        client = MagicMock()
        client.get.return_value = _configmap(["not-an-object"])
        with pytest.raises(ValueError, match="must be objects"):
            rrv.read_history(client)


# --- find_cached_entry ---


class TestFindCachedEntry:
    def test_hit_returns_full_entry(self):
        e1 = _entry(_SHA_A, "2.335.0", "sha256:aaa")
        e2 = _entry(_SHA_B, "2.334.0", "sha256:bbb")
        assert rrv.find_cached_entry([e1, e2], _SHA_B) == e2

    def test_miss_returns_none(self):
        assert rrv.find_cached_entry([_entry(_SHA_A, "2.335.0", "sha256:aaa")], _SHA_C) is None

    def test_empty_returns_none(self):
        assert rrv.find_cached_entry([], _SHA_A) is None


# --- update_history ---


class TestUpdateHistory:
    def test_prepends_new_entry(self, fixed_now):
        existing = [_entry(_SHA_A, "2.334.0", "sha256:bbb", "2026-06-08T09:03:55Z")]
        result = rrv.update_history(existing, _SHA_B, "2.335.0", "sha256:aaa", fixed_now)
        assert result[0] == _entry(_SHA_B, "2.335.0", "sha256:aaa", "2026-06-15T17:42:11Z")
        assert result[1] == existing[0]

    def test_dedupes_existing_sha(self, fixed_now):
        existing = [
            _entry(_SHA_A, "2.334.0", "sha256:old1", "2026-05-01T00:00:00Z"),
            _entry(_SHA_B, "2.335.0", "sha256:zzz", "2026-06-01T00:00:00Z"),
            _entry(_SHA_C, "2.336.0", "sha256:other", "2026-06-10T00:00:00Z"),
        ]
        result = rrv.update_history(existing, _SHA_B, "2.337.0", "sha256:new", fixed_now)
        shas = [e["osdc_sha"] for e in result]
        assert shas.count(_SHA_B) == 1
        assert result[0]["osdc_sha"] == _SHA_B
        assert result[0]["tag"] == "2.337.0"
        assert result[0]["digest"] == "sha256:new"
        assert result[0]["resolved_at"] == "2026-06-15T17:42:11Z"
        assert shas == [_SHA_B, _SHA_A, _SHA_C]

    def test_does_not_dedupe_on_tag(self, fixed_now):
        existing = [
            _entry(_SHA_A, "2.335.0", "sha256:aaa", "2026-05-01T00:00:00Z"),
            _entry(_SHA_B, "2.335.0", "sha256:bbb", "2026-06-01T00:00:00Z"),
        ]
        result = rrv.update_history(existing, _SHA_C, "2.335.0", "sha256:ccc", fixed_now)
        tags = [e["tag"] for e in result]
        assert tags == ["2.335.0", "2.335.0", "2.335.0"]
        shas = [e["osdc_sha"] for e in result]
        assert shas == [_SHA_C, _SHA_A, _SHA_B]

    def test_trims_to_max(self, fixed_now):
        existing = [_entry(f"{i:040x}", f"2.{i}.0", f"sha256:{i:03d}", "") for i in range(25)]
        result = rrv.update_history(existing, _SHA_A, "9.0.0", "sha256:new", fixed_now)
        assert len(result) == rrv.HISTORY_MAX
        assert result[0]["osdc_sha"] == _SHA_A
        assert result[0]["tag"] == "9.0.0"


# --- resolve_digest ---


class TestResolveDigest:
    def test_strips_and_returns_digest(self, patched):
        patched.setattr(rrv.subprocess, "run", lambda *a, **kw: _crane_result("sha256:abc"))
        assert rrv.resolve_digest("2.335.0") == "sha256:abc"

    def test_unexpected_output_raises(self, patched):
        patched.setattr(
            rrv.subprocess,
            "run",
            lambda *a, **kw: subprocess.CompletedProcess(args=[], returncode=0, stdout="not-a-digest\n", stderr=""),
        )
        with pytest.raises(ValueError, match="unexpected"):
            rrv.resolve_digest("2.335.0")

    def test_crane_nonzero_exit_raises_called_process_error(self, patched):
        def fake_run(*a, **kw):
            raise subprocess.CalledProcessError(returncode=1, cmd=a[0], stderr="unauthorized\n")

        patched.setattr(rrv.subprocess, "run", fake_run)
        with pytest.raises(subprocess.CalledProcessError):
            rrv.resolve_digest("2.335.0")


# --- write_history ---


class TestWriteHistory:
    def test_creates_when_not_exists(self):
        client = MagicMock()
        history = [_entry(_SHA_A, "2.335.0", "sha256:aaa", "2026-06-15T17:42:11Z")]
        rrv.write_history(client, history, cm_exists=False, resource_version=None)
        client.create.assert_called_once()
        client.replace.assert_not_called()
        cm_arg = client.create.call_args[0][0]
        assert cm_arg.metadata.name == rrv.CM_NAME
        assert cm_arg.metadata.namespace == rrv.CM_NAMESPACE
        assert cm_arg.metadata.labels == rrv.CM_LABELS
        assert getattr(cm_arg.metadata, "resourceVersion", None) is None
        assert json.loads(cm_arg.data[rrv.CM_KEY]) == history

    def test_replaces_when_exists_sets_resource_version(self):
        client = MagicMock()
        rrv.write_history(client, [_entry(_SHA_A, "x", "sha256:y", "z")], cm_exists=True, resource_version="42")
        client.replace.assert_called_once()
        client.create.assert_not_called()
        cm_arg = client.replace.call_args[0][0]
        assert cm_arg.metadata.resourceVersion == "42"

    def test_create_ignores_resource_version_when_not_exists(self):
        client = MagicMock()
        rrv.write_history(client, [_entry(_SHA_A, "x", "sha256:y", "z")], cm_exists=False, resource_version="99")
        client.create.assert_called_once()
        cm_arg = client.create.call_args[0][0]
        assert getattr(cm_arg.metadata, "resourceVersion", None) is None

    def test_replace_with_none_resource_version_omits_field(self):
        client = MagicMock()
        rrv.write_history(client, [_entry(_SHA_A, "x", "sha256:y", "z")], cm_exists=True, resource_version=None)
        client.replace.assert_called_once()
        cm_arg = client.replace.call_args[0][0]
        assert getattr(cm_arg.metadata, "resourceVersion", None) is None


# --- main() end-to-end (mocking module boundaries) ---


class _Env:
    """Holds the mocked module boundaries for an end-to-end main() run."""

    def __init__(
        self,
        monkeypatch,
        *,
        sha: str = _SHA_A,
        tag: str = "2.335.0",
        digest: str = "sha256:abc",
        history: list[dict[str, str]] | None = None,
        history_exists: bool = True,
        resource_version: str | None = "100",
    ):
        self.client = MagicMock()
        self.fetched_token: list[str | None] = []
        self.crane_calls: list[str] = []
        self.history = history if history is not None else []
        self.history_exists = history_exists
        self.resource_version = resource_version if history_exists else None
        self.sha = sha

        def fake_fetch(token):
            self.fetched_token.append(token)
            return tag

        def fake_read(_client):
            return list(self.history), self.history_exists, self.resource_version

        def fake_resolve(_tag):
            self.crane_calls.append(_tag)
            return digest

        monkeypatch.setattr(rrv, "fetch_latest_tag", fake_fetch)
        monkeypatch.setattr(rrv, "read_history", fake_read)
        monkeypatch.setattr(rrv, "resolve_digest", fake_resolve)
        monkeypatch.setattr(rrv, "build_client", lambda: self.client)
        monkeypatch.setattr(rrv, "osdc_sha", lambda: self.sha)
        monkeypatch.setattr(rrv, "now_utc", lambda: datetime(2026, 6, 15, 17, 42, 11, tzinfo=UTC))


class TestMain:
    def test_usage_when_no_args(self, capsys):
        rc = rrv.main(["resolve_runner_version.py"])
        assert rc == 2
        assert "usage" in capsys.readouterr().err

    def test_configmap_missing_first_deploy(self, monkeypatch, capsys):
        env = _Env(monkeypatch, sha=_SHA_A, history=[], history_exists=False)
        rc = rrv.main(["resolve_runner_version.py", "test-cluster"])
        captured = capsys.readouterr()
        assert rc == 0
        assert captured.out.strip() == "ghcr.io/actions/actions-runner:2.335.0@sha256:abc"
        env.client.create.assert_called_once()
        env.client.replace.assert_not_called()
        assert env.crane_calls == ["2.335.0"]
        cm = env.client.create.call_args[0][0]
        history = json.loads(cm.data[rrv.CM_KEY])
        assert history[0]["osdc_sha"] == _SHA_A
        assert history[0]["tag"] == "2.335.0"
        assert history[0]["digest"] == "sha256:abc"

    def test_sha_hit_no_api_no_crane_no_write(self, monkeypatch, capsys):
        env = _Env(
            monkeypatch,
            sha=_SHA_A,
            history=[_entry(_SHA_A, "2.334.0", "sha256:cached", "2026-05-01T00:00:00Z")],
        )
        rc = rrv.main(["resolve_runner_version.py", "c"])
        captured = capsys.readouterr()
        assert rc == 0
        assert captured.out.strip() == "ghcr.io/actions/actions-runner:2.334.0@sha256:cached"
        env.client.create.assert_not_called()
        env.client.replace.assert_not_called()
        assert env.crane_calls == []
        assert env.fetched_token == []

    def test_sha_miss_prepends_and_writes(self, monkeypatch, capsys):
        env = _Env(
            monkeypatch,
            sha=_SHA_C,
            tag="2.335.0",
            digest="sha256:newdigest",
            history=[_entry(_SHA_A, "2.334.0", "sha256:old", "2026-05-01T00:00:00Z")],
            resource_version="500",
        )
        rc = rrv.main(["resolve_runner_version.py", "c"])
        assert rc == 0
        env.client.replace.assert_called_once()
        cm = env.client.replace.call_args[0][0]
        assert cm.metadata.resourceVersion == "500"
        history = json.loads(cm.data[rrv.CM_KEY])
        assert history[0] == _entry(_SHA_C, "2.335.0", "sha256:newdigest", "2026-06-15T17:42:11Z")
        assert history[1]["osdc_sha"] == _SHA_A
        capsys.readouterr()

    def test_sha_miss_with_existing_history_for_other_shas(self, monkeypatch):
        existing = [
            _entry(_SHA_A, "2.333.0", "sha256:a", "2026-04-01T00:00:00Z"),
            _entry(_SHA_B, "2.334.0", "sha256:b", "2026-05-01T00:00:00Z"),
        ]
        env = _Env(monkeypatch, sha=_SHA_C, tag="2.335.0", digest="sha256:c", history=existing)
        rc = rrv.main(["resolve_runner_version.py", "c"])
        assert rc == 0
        cm = env.client.replace.call_args[0][0]
        history = json.loads(cm.data[rrv.CM_KEY])
        assert len(history) == 3
        assert history[0]["osdc_sha"] == _SHA_C
        assert history[1] == existing[0]
        assert history[2] == existing[1]

    def test_history_trimmed_to_20_after_new_miss(self, monkeypatch):
        existing = [_entry(f"{i:040x}", f"2.{i}.0", f"sha256:{i:03d}", "") for i in range(20)]
        env = _Env(monkeypatch, sha=_SHA_A, tag="new", digest="sha256:n", history=existing)
        rc = rrv.main(["resolve_runner_version.py", "c"])
        assert rc == 0
        cm = env.client.replace.call_args[0][0]
        history = json.loads(cm.data[rrv.CM_KEY])
        assert len(history) == rrv.HISTORY_MAX
        assert history[0]["osdc_sha"] == _SHA_A
        assert history[0]["tag"] == "new"

    def test_dedupe_on_osdc_sha_not_tag(self, monkeypatch):
        existing = [
            _entry(_SHA_A, "2.335.0", "sha256:a", "2026-04-01T00:00:00Z"),
            _entry(_SHA_B, "2.335.0", "sha256:b", "2026-05-01T00:00:00Z"),
        ]
        env = _Env(monkeypatch, sha=_SHA_C, tag="2.335.0", digest="sha256:c", history=existing)
        rc = rrv.main(["resolve_runner_version.py", "c"])
        assert rc == 0
        cm = env.client.replace.call_args[0][0]
        history = json.loads(cm.data[rrv.CM_KEY])
        assert len(history) == 3
        tags = [e["tag"] for e in history]
        assert tags == ["2.335.0", "2.335.0", "2.335.0"]
        shas = [e["osdc_sha"] for e in history]
        assert shas == [_SHA_C, _SHA_A, _SHA_B]

    def test_readonly_with_matching_sha_returns_cached(self, monkeypatch, capsys):
        monkeypatch.setenv("OSDC_RESOLVER_READONLY", "1")
        env = _Env(
            monkeypatch,
            sha=_SHA_A,
            history=[
                _entry(_SHA_A, "2.336.0", "sha256:new", "2026-06-10T00:00:00Z"),
                _entry(_SHA_B, "2.335.0", "sha256:old", "2026-05-01T00:00:00Z"),
            ],
        )
        rc = rrv.main(["resolve_runner_version.py", "c"])
        captured = capsys.readouterr()
        assert rc == 0
        assert captured.out.strip() == "ghcr.io/actions/actions-runner:2.336.0@sha256:new"
        assert env.fetched_token == []
        assert env.crane_calls == []
        env.client.create.assert_not_called()
        env.client.replace.assert_not_called()

    def test_readonly_with_unknown_sha_falls_back_to_newest(self, monkeypatch, capsys):
        monkeypatch.setenv("OSDC_RESOLVER_READONLY", "1")
        newest = _entry(_SHA_A, "2.336.0", "sha256:newest", "2026-06-10T00:00:00Z")
        older = _entry(_SHA_B, "2.335.0", "sha256:older", "2026-05-01T00:00:00Z")
        env = _Env(monkeypatch, sha=_SHA_C, history=[newest, older])
        rc = rrv.main(["resolve_runner_version.py", "c"])
        captured = capsys.readouterr()
        assert rc == 0
        assert captured.out.strip() == "ghcr.io/actions/actions-runner:2.336.0@sha256:newest"
        assert "OSDC_RESOLVER_READONLY" in captured.err
        assert "falling back to newest entry" in captured.err
        assert _SHA_A in captured.err
        assert "2026-06-10T00:00:00Z" in captured.err
        assert env.fetched_token == []
        assert env.crane_calls == []
        env.client.create.assert_not_called()
        env.client.replace.assert_not_called()

    def test_readonly_empty_history_fails(self, monkeypatch, capsys):
        monkeypatch.setenv("OSDC_RESOLVER_READONLY", "1")
        env = _Env(monkeypatch, sha=_SHA_A, history=[], history_exists=False)
        rc = rrv.main(["resolve_runner_version.py", "c"])
        captured = capsys.readouterr()
        assert rc == 1
        assert "OSDC_RESOLVER_READONLY" in captured.err
        assert env.fetched_token == []
        assert env.crane_calls == []
        env.client.create.assert_not_called()

    def test_github_api_http_error_exits_nonzero_no_mutation(self, monkeypatch, capsys):
        client = MagicMock()
        monkeypatch.setattr(rrv, "build_client", lambda: client)
        monkeypatch.setattr(rrv, "osdc_sha", lambda: _SHA_A)
        monkeypatch.setattr(rrv, "read_history", lambda _c: ([], False, None))

        def fake_fetch(_token):
            raise requests.HTTPError("503 Service Unavailable")

        monkeypatch.setattr(rrv, "fetch_latest_tag", fake_fetch)
        rc = rrv.main(["resolve_runner_version.py", "c"])
        assert rc == 1
        assert "GitHub API" in capsys.readouterr().err
        client.create.assert_not_called()
        client.replace.assert_not_called()

    def test_github_api_missing_tag_name_exits_nonzero(self, monkeypatch, capsys, patched):
        client = MagicMock()
        monkeypatch.setattr(rrv, "build_client", lambda: client)
        monkeypatch.setattr(rrv, "osdc_sha", lambda: _SHA_A)
        monkeypatch.setattr(rrv, "read_history", lambda _c: ([], False, None))
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {}
        patched.setattr(rrv.requests, "get", lambda *a, **kw: resp)
        rc = rrv.main(["resolve_runner_version.py", "c"])
        assert rc == 1
        assert "tag_name" in capsys.readouterr().err
        client.create.assert_not_called()
        client.replace.assert_not_called()

    def test_crane_failure_exits_nonzero_no_mutation(self, monkeypatch, capsys):
        client = MagicMock()
        monkeypatch.setattr(rrv, "build_client", lambda: client)
        monkeypatch.setattr(rrv, "osdc_sha", lambda: _SHA_A)
        monkeypatch.setattr(rrv, "fetch_latest_tag", lambda _t: "2.335.0")
        monkeypatch.setattr(rrv, "read_history", lambda _c: ([], False, None))

        def fake_resolve(_tag):
            raise subprocess.CalledProcessError(returncode=1, cmd=["crane"], stderr="unauthorized")

        monkeypatch.setattr(rrv, "resolve_digest", fake_resolve)
        rc = rrv.main(["resolve_runner_version.py", "c"])
        assert rc == 1
        assert "crane" in capsys.readouterr().err
        client.create.assert_not_called()
        client.replace.assert_not_called()

    def test_git_log_empty_exits_nonzero(self, monkeypatch, capsys):
        client = MagicMock()
        monkeypatch.setattr(rrv, "build_client", lambda: client)

        def fake_sha():
            raise ValueError("git log returned no commit history under /fake/root")

        monkeypatch.setattr(rrv, "osdc_sha", fake_sha)
        rc = rrv.main(["resolve_runner_version.py", "c"])
        assert rc == 1
        assert "git log" in capsys.readouterr().err
        client.create.assert_not_called()

    def test_malformed_configmap_history_exits_nonzero_no_mutation(self, monkeypatch, capsys):
        client = MagicMock()
        client.get.return_value = _configmap("not json {")
        monkeypatch.setattr(rrv, "build_client", lambda: client)
        monkeypatch.setattr(rrv, "osdc_sha", lambda: _SHA_A)
        monkeypatch.setattr(rrv, "fetch_latest_tag", lambda _t: "2.335.0")
        rc = rrv.main(["resolve_runner_version.py", "c"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "history.json" in err or "JSON" in err
        client.create.assert_not_called()
        client.replace.assert_not_called()

    def test_configmap_write_failure_propagates_exit_nonzero(self, monkeypatch, capsys):
        client = MagicMock()
        client.create.side_effect = _api_error(500)
        monkeypatch.setattr(rrv, "build_client", lambda: client)
        monkeypatch.setattr(rrv, "osdc_sha", lambda: _SHA_A)
        monkeypatch.setattr(rrv, "fetch_latest_tag", lambda _t: "2.335.0")
        monkeypatch.setattr(rrv, "read_history", lambda _c: ([], False, None))
        monkeypatch.setattr(rrv, "resolve_digest", lambda _t: "sha256:abc")
        rc = rrv.main(["resolve_runner_version.py", "c"])
        assert rc == 1
        assert "kubernetes API" in capsys.readouterr().err

    def test_github_token_env_var_passed_through(self, monkeypatch):
        client = MagicMock()
        monkeypatch.setattr(rrv, "build_client", lambda: client)
        monkeypatch.setattr(rrv, "osdc_sha", lambda: _SHA_A)
        seen = []

        def fake_fetch(token):
            seen.append(token)
            return "2.335.0"

        monkeypatch.setattr(rrv, "fetch_latest_tag", fake_fetch)
        monkeypatch.setattr(rrv, "read_history", lambda _c: ([], False, None))
        monkeypatch.setattr(rrv, "resolve_digest", lambda _t: "sha256:abc")
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_test_token")
        rc = rrv.main(["resolve_runner_version.py", "c"])
        assert rc == 0
        assert seen == ["ghp_test_token"]

    def test_github_token_absent(self, monkeypatch):
        client = MagicMock()
        monkeypatch.setattr(rrv, "build_client", lambda: client)
        monkeypatch.setattr(rrv, "osdc_sha", lambda: _SHA_A)
        seen = []

        def fake_fetch(token):
            seen.append(token)
            return "2.335.0"

        monkeypatch.setattr(rrv, "fetch_latest_tag", fake_fetch)
        monkeypatch.setattr(rrv, "read_history", lambda _c: ([], False, None))
        monkeypatch.setattr(rrv, "resolve_digest", lambda _t: "sha256:abc")
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        rc = rrv.main(["resolve_runner_version.py", "c"])
        assert rc == 0
        assert seen == [None]


# --- concurrency retry loop in _run ---


class TestWriteRetryConcurrency:
    def _common_patches(self, monkeypatch, sha):
        monkeypatch.setattr(rrv, "fetch_latest_tag", lambda _t: "2.335.0")
        monkeypatch.setattr(rrv, "resolve_digest", lambda _t: "sha256:our")
        monkeypatch.setattr(rrv, "osdc_sha", lambda: sha)
        monkeypatch.setattr(rrv, "now_utc", lambda: datetime(2026, 6, 15, 17, 42, 11, tzinfo=UTC))
        monkeypatch.delenv("OSDC_RESOLVER_READONLY", raising=False)

    def test_write_409_then_winner_pinned_returns_winner(self, monkeypatch, capsys):
        self._common_patches(monkeypatch, _SHA_A)
        client = MagicMock()
        winner_entry = _entry(_SHA_A, "2.335.0", "sha256:winner", "2026-06-15T17:00:00Z")
        reads = [
            ([], True, "100"),
            ([winner_entry], True, "101"),
        ]
        monkeypatch.setattr(rrv, "read_history", MagicMock(side_effect=reads))
        writes = MagicMock(side_effect=[_api_error(409)])

        def fake_write(_client, history, cm_exists, rv):
            return writes(history, cm_exists, rv)

        monkeypatch.setattr(rrv, "write_history", fake_write)
        monkeypatch.setattr(rrv, "build_client", lambda: client)

        rc = rrv.main(["resolve_runner_version.py", "c"])
        captured = capsys.readouterr()
        assert rc == 0
        assert captured.out.strip() == "ghcr.io/actions/actions-runner:2.335.0@sha256:winner"
        assert writes.call_count == 1

    def test_write_409_then_winner_pinned_different_image_returns_winner(self, monkeypatch, capsys):
        self._common_patches(monkeypatch, _SHA_A)
        client = MagicMock()
        winner_entry = _entry(_SHA_A, "2.999.0", "sha256:totally-different", "2026-06-15T17:00:00Z")
        reads = [
            ([], True, "100"),
            ([winner_entry], True, "101"),
        ]
        monkeypatch.setattr(rrv, "read_history", MagicMock(side_effect=reads))
        write_calls = []

        def fake_write(_client, history, cm_exists, rv):
            write_calls.append((history, cm_exists, rv))
            if len(write_calls) == 1:
                raise _api_error(409)

        monkeypatch.setattr(rrv, "write_history", fake_write)
        monkeypatch.setattr(rrv, "build_client", lambda: client)

        rc = rrv.main(["resolve_runner_version.py", "c"])
        captured = capsys.readouterr()
        assert rc == 0
        assert captured.out.strip() == "ghcr.io/actions/actions-runner:2.999.0@sha256:totally-different"
        assert len(write_calls) == 1

    def test_write_409_then_we_still_miss_retry_succeeds(self, monkeypatch, capsys):
        self._common_patches(monkeypatch, _SHA_A)
        client = MagicMock()
        unrelated_entry = _entry(_SHA_B, "2.334.0", "sha256:other", "2026-06-15T17:00:00Z")
        reads = [
            ([], True, "100"),
            ([unrelated_entry], True, "101"),
        ]
        monkeypatch.setattr(rrv, "read_history", MagicMock(side_effect=reads))
        write_calls = []

        def fake_write(_client, history, cm_exists, rv):
            write_calls.append((history, cm_exists, rv))
            if len(write_calls) == 1:
                raise _api_error(409)

        monkeypatch.setattr(rrv, "write_history", fake_write)
        monkeypatch.setattr(rrv, "build_client", lambda: client)

        rc = rrv.main(["resolve_runner_version.py", "c"])
        captured = capsys.readouterr()
        assert rc == 0
        assert captured.out.strip() == "ghcr.io/actions/actions-runner:2.335.0@sha256:our"
        assert len(write_calls) == 2
        assert write_calls[1][2] == "101"
        retry_history = write_calls[1][0]
        assert retry_history[0]["osdc_sha"] == _SHA_A
        assert retry_history[0]["digest"] == "sha256:our"
        assert any(e["osdc_sha"] == _SHA_B for e in retry_history)

    def test_write_409_retries_exhausted(self, monkeypatch, capsys):
        self._common_patches(monkeypatch, _SHA_A)
        client = MagicMock()
        reads = [([], True, str(100 + i)) for i in range(rrv.MAX_WRITE_ATTEMPTS + 1)]
        monkeypatch.setattr(rrv, "read_history", MagicMock(side_effect=reads))
        write_calls = []

        def fake_write(_client, history, cm_exists, rv):
            write_calls.append((history, cm_exists, rv))
            raise _api_error(409)

        monkeypatch.setattr(rrv, "write_history", fake_write)
        monkeypatch.setattr(rrv, "build_client", lambda: client)

        rc = rrv.main(["resolve_runner_version.py", "c"])
        captured = capsys.readouterr()
        assert rc == 1
        assert len(write_calls) == rrv.MAX_WRITE_ATTEMPTS
        assert "kubernetes API" in captured.err

    def test_non_409_apierror_propagates(self, monkeypatch, capsys):
        self._common_patches(monkeypatch, _SHA_A)
        client = MagicMock()
        monkeypatch.setattr(rrv, "read_history", lambda _c: ([], True, "100"))
        write_calls = []

        def fake_write(_client, history, cm_exists, rv):
            write_calls.append((history, cm_exists, rv))
            raise _api_error(403)

        monkeypatch.setattr(rrv, "write_history", fake_write)
        monkeypatch.setattr(rrv, "build_client", lambda: client)

        rc = rrv.main(["resolve_runner_version.py", "c"])
        captured = capsys.readouterr()
        assert rc == 1
        assert len(write_calls) == 1
        assert "kubernetes API" in captured.err


# --- module-boundary helpers (small smoke for build_client / now_utc) ---


class TestModuleBoundaries:
    def test_now_utc_returns_aware_datetime(self):
        result = rrv.now_utc()
        assert result.tzinfo is not None

    def test_build_client_constructs_client(self):
        with patch.object(rrv, "Client") as mock_client_cls, patch.object(rrv, "_force_ipv4") as mock_force:
            rrv.build_client()
            mock_force.assert_called_once_with()
            mock_client_cls.assert_called_once_with()
