"""Tests for resolve_runner_version.py."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
import requests
import resolve_runner_version as rrv
from lightkube.core.exceptions import ApiError


def _api_error(code: int) -> ApiError:
    err = ApiError.__new__(ApiError)
    err.status = MagicMock(code=code)
    return err


def _release_response(tag: str = "v2.335.0") -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {"tag_name": tag}
    return resp


def _configmap(history: list[dict[str, str]] | str) -> MagicMock:
    cm = MagicMock()
    cm.data = {rrv.CM_KEY: history if isinstance(history, str) else json.dumps(history)}
    return cm


def _crane_result(digest: str = "sha256:abc123") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["crane", "digest", "x"], returncode=0, stdout=f"{digest}\n", stderr="")


@pytest.fixture
def fixed_now():
    return datetime(2026, 6, 15, 17, 42, 11, tzinfo=UTC)


@pytest.fixture
def patched(monkeypatch, fixed_now):
    monkeypatch.setattr(rrv, "now_utc", lambda: fixed_now)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    return monkeypatch


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
    def test_404_returns_empty_and_false(self):
        client = MagicMock()
        client.get.side_effect = _api_error(404)
        history, exists = rrv.read_history(client)
        assert history == []
        assert exists is False

    def test_non_404_api_error_reraised(self):
        client = MagicMock()
        client.get.side_effect = _api_error(403)
        with pytest.raises(ApiError):
            rrv.read_history(client)

    def test_parses_valid_history(self):
        entries = [{"tag": "2.335.0", "digest": "sha256:aaa", "resolved_at": "2026-06-15T17:42:11Z"}]
        client = MagicMock()
        client.get.return_value = _configmap(entries)
        history, exists = rrv.read_history(client)
        assert history == entries
        assert exists is True

    def test_missing_data_key_returns_empty_exists(self):
        client = MagicMock()
        cm = MagicMock()
        cm.data = {}
        client.get.return_value = cm
        history, exists = rrv.read_history(client)
        assert history == []
        assert exists is True

    def test_none_data_returns_empty_exists(self):
        client = MagicMock()
        cm = MagicMock()
        cm.data = None
        client.get.return_value = cm
        history, exists = rrv.read_history(client)
        assert history == []
        assert exists is True

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

    def test_entry_missing_required_fields_raises(self):
        client = MagicMock()
        client.get.return_value = _configmap([{"tag": "2.0"}])
        with pytest.raises(ValueError, match="must be objects"):
            rrv.read_history(client)


# --- find_cached_digest, update_history, resolve_digest ---


class TestFindCachedDigest:
    def test_hit(self):
        history = [{"tag": "2.335.0", "digest": "sha256:aaa"}, {"tag": "2.334.0", "digest": "sha256:bbb"}]
        assert rrv.find_cached_digest(history, "2.334.0") == "sha256:bbb"

    def test_miss(self):
        assert rrv.find_cached_digest([{"tag": "2.335.0", "digest": "sha256:aaa"}], "9.9.9") is None

    def test_empty(self):
        assert rrv.find_cached_digest([], "2.335.0") is None


class TestUpdateHistory:
    def test_prepends_new_entry(self, fixed_now):
        existing = [{"tag": "2.334.0", "digest": "sha256:bbb", "resolved_at": "2026-06-08T09:03:55Z"}]
        result = rrv.update_history(existing, "2.335.0", "sha256:aaa", fixed_now)
        assert result[0] == {"tag": "2.335.0", "digest": "sha256:aaa", "resolved_at": "2026-06-15T17:42:11Z"}
        assert result[1] == existing[0]

    def test_dedupes_existing_tag(self, fixed_now):
        existing = [
            {"tag": "2.334.0", "digest": "sha256:old1", "resolved_at": "2026-05-01T00:00:00Z"},
            {"tag": "2.335.0", "digest": "sha256:zzz", "resolved_at": "2026-06-01T00:00:00Z"},
            {"tag": "2.336.0", "digest": "sha256:other", "resolved_at": "2026-06-10T00:00:00Z"},
        ]
        result = rrv.update_history(existing, "2.335.0", "sha256:new", fixed_now)
        tags = [e["tag"] for e in result]
        assert tags.count("2.335.0") == 1
        assert result[0]["digest"] == "sha256:new"
        assert result[0]["resolved_at"] == "2026-06-15T17:42:11Z"
        assert tags == ["2.335.0", "2.334.0", "2.336.0"]

    def test_trims_to_max(self, fixed_now):
        existing = [{"tag": f"2.{i}.0", "digest": f"sha256:{i:03d}", "resolved_at": ""} for i in range(25)]
        result = rrv.update_history(existing, "9.0.0", "sha256:new", fixed_now)
        assert len(result) == rrv.HISTORY_MAX
        assert result[0]["tag"] == "9.0.0"
        assert result[-1]["tag"] == "2.18.0"


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
        history = [{"tag": "2.335.0", "digest": "sha256:aaa", "resolved_at": "2026-06-15T17:42:11Z"}]
        rrv.write_history(client, history, cm_exists=False)
        client.create.assert_called_once()
        client.replace.assert_not_called()
        cm_arg = client.create.call_args[0][0]
        assert cm_arg.metadata.name == rrv.CM_NAME
        assert cm_arg.metadata.namespace == rrv.CM_NAMESPACE
        assert cm_arg.metadata.labels == rrv.CM_LABELS
        assert json.loads(cm_arg.data[rrv.CM_KEY]) == history

    def test_replaces_when_exists(self):
        client = MagicMock()
        rrv.write_history(client, [{"tag": "x", "digest": "sha256:y", "resolved_at": "z"}], cm_exists=True)
        client.replace.assert_called_once()
        client.create.assert_not_called()


# --- main() end-to-end (mocking module boundaries) ---


class _Env:
    """Holds the mocked module boundaries for an end-to-end main() run."""

    def __init__(self, monkeypatch, *, tag="2.335.0", digest="sha256:abc", history=None, history_exists=True):
        self.client = MagicMock()
        self.fetched_token = []
        self.crane_calls = []
        self.history = history if history is not None else []
        self.history_exists = history_exists

        def fake_fetch(token):
            self.fetched_token.append(token)
            return tag

        def fake_read(_client):
            return list(self.history), self.history_exists

        def fake_resolve(_tag):
            self.crane_calls.append(_tag)
            return digest

        monkeypatch.setattr(rrv, "fetch_latest_tag", fake_fetch)
        monkeypatch.setattr(rrv, "read_history", fake_read)
        monkeypatch.setattr(rrv, "resolve_digest", fake_resolve)
        monkeypatch.setattr(rrv, "build_client", lambda: self.client)
        monkeypatch.setattr(rrv, "now_utc", lambda: datetime(2026, 6, 15, 17, 42, 11, tzinfo=UTC))


class TestMain:
    def test_usage_when_no_args(self, capsys):
        rc = rrv.main(["resolve_runner_version.py"])
        assert rc == 2
        assert "usage" in capsys.readouterr().err

    def test_configmap_missing_first_deploy(self, monkeypatch, capsys):
        env = _Env(monkeypatch, history=[], history_exists=False)
        rc = rrv.main(["resolve_runner_version.py", "test-cluster"])
        captured = capsys.readouterr()
        assert rc == 0
        assert captured.out.strip() == "ghcr.io/actions/actions-runner:2.335.0@sha256:abc"
        env.client.create.assert_called_once()
        env.client.replace.assert_not_called()
        assert env.crane_calls == ["2.335.0"]

    def test_tag_hit_no_crane_no_write(self, monkeypatch, capsys):
        env = _Env(
            monkeypatch,
            tag="2.334.0",
            history=[{"tag": "2.334.0", "digest": "sha256:cached", "resolved_at": "2026-05-01T00:00:00Z"}],
        )
        rc = rrv.main(["resolve_runner_version.py", "c"])
        captured = capsys.readouterr()
        assert rc == 0
        assert captured.out.strip() == "ghcr.io/actions/actions-runner:2.334.0@sha256:cached"
        env.client.create.assert_not_called()
        env.client.replace.assert_not_called()
        assert env.crane_calls == []

    def test_tag_miss_prepends_and_writes(self, monkeypatch, capsys):
        env = _Env(
            monkeypatch,
            tag="2.335.0",
            digest="sha256:newdigest",
            history=[{"tag": "2.334.0", "digest": "sha256:old", "resolved_at": "2026-05-01T00:00:00Z"}],
        )
        rc = rrv.main(["resolve_runner_version.py", "c"])
        assert rc == 0
        env.client.replace.assert_called_once()
        cm = env.client.replace.call_args[0][0]
        history = json.loads(cm.data[rrv.CM_KEY])
        assert history[0] == {"tag": "2.335.0", "digest": "sha256:newdigest", "resolved_at": "2026-06-15T17:42:11Z"}
        assert history[1]["tag"] == "2.334.0"
        capsys.readouterr()

    def test_25_existing_entries_trimmed_to_20(self, monkeypatch):
        existing = [{"tag": f"old-{i}", "digest": f"sha256:{i:03d}", "resolved_at": ""} for i in range(25)]
        env = _Env(monkeypatch, tag="new", digest="sha256:n", history=existing)
        rc = rrv.main(["resolve_runner_version.py", "c"])
        assert rc == 0
        cm = env.client.replace.call_args[0][0]
        history = json.loads(cm.data[rrv.CM_KEY])
        assert len(history) == rrv.HISTORY_MAX
        assert history[0]["tag"] == "new"

    def test_duplicate_tag_deduped_newest_wins(self, monkeypatch):
        existing = [
            {"tag": "a", "digest": "sha256:a1", "resolved_at": "2026-01-01T00:00:00Z"},
            {"tag": "new", "digest": "sha256:stale-dup", "resolved_at": "2026-02-01T00:00:00Z"},
            {"tag": "b", "digest": "sha256:b1", "resolved_at": "2026-03-01T00:00:00Z"},
            {"tag": "new", "digest": "sha256:also-stale", "resolved_at": "2026-04-01T00:00:00Z"},
        ]
        env = _Env(monkeypatch, tag="new", digest="sha256:fresh", history=existing)
        env.client.get = MagicMock(return_value=_configmap(existing))

        def fake_read(_client):
            return list(existing), True

        monkeypatch.setattr(rrv, "read_history", fake_read)

        def fake_find(_history, _tag):
            return None

        monkeypatch.setattr(rrv, "find_cached_digest", fake_find)

        rc = rrv.main(["resolve_runner_version.py", "c"])
        assert rc == 0
        cm = env.client.replace.call_args[0][0]
        history = json.loads(cm.data[rrv.CM_KEY])
        tags = [e["tag"] for e in history]
        assert tags.count("new") == 1
        assert history[0]["tag"] == "new"
        assert history[0]["digest"] == "sha256:fresh"
        assert tags == ["new", "a", "b"]

    def test_github_api_http_error_exits_nonzero_no_mutation(self, monkeypatch, capsys):
        client = MagicMock()
        monkeypatch.setattr(rrv, "build_client", lambda: client)

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
        monkeypatch.setattr(rrv, "fetch_latest_tag", lambda _t: "2.335.0")
        monkeypatch.setattr(rrv, "read_history", lambda _c: ([], False))

        def fake_resolve(_tag):
            raise subprocess.CalledProcessError(returncode=1, cmd=["crane"], stderr="unauthorized")

        monkeypatch.setattr(rrv, "resolve_digest", fake_resolve)
        rc = rrv.main(["resolve_runner_version.py", "c"])
        assert rc == 1
        assert "crane" in capsys.readouterr().err
        client.create.assert_not_called()
        client.replace.assert_not_called()

    def test_malformed_configmap_history_exits_nonzero_no_mutation(self, monkeypatch, capsys):
        client = MagicMock()
        client.get.return_value = _configmap("not json {")
        monkeypatch.setattr(rrv, "build_client", lambda: client)
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
        monkeypatch.setattr(rrv, "fetch_latest_tag", lambda _t: "2.335.0")
        monkeypatch.setattr(rrv, "read_history", lambda _c: ([], False))
        monkeypatch.setattr(rrv, "resolve_digest", lambda _t: "sha256:abc")
        rc = rrv.main(["resolve_runner_version.py", "c"])
        assert rc == 1
        assert "kubernetes API" in capsys.readouterr().err

    def test_github_token_env_var_passed_through(self, monkeypatch):
        client = MagicMock()
        monkeypatch.setattr(rrv, "build_client", lambda: client)
        seen = []

        def fake_fetch(token):
            seen.append(token)
            return "2.335.0"

        monkeypatch.setattr(rrv, "fetch_latest_tag", fake_fetch)
        monkeypatch.setattr(rrv, "read_history", lambda _c: ([], False))
        monkeypatch.setattr(rrv, "resolve_digest", lambda _t: "sha256:abc")
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_test_token")
        rc = rrv.main(["resolve_runner_version.py", "c"])
        assert rc == 0
        assert seen == ["ghp_test_token"]

    def test_github_token_absent(self, monkeypatch):
        client = MagicMock()
        monkeypatch.setattr(rrv, "build_client", lambda: client)
        seen = []

        def fake_fetch(token):
            seen.append(token)
            return "2.335.0"

        monkeypatch.setattr(rrv, "fetch_latest_tag", fake_fetch)
        monkeypatch.setattr(rrv, "read_history", lambda _c: ([], False))
        monkeypatch.setattr(rrv, "resolve_digest", lambda _t: "sha256:abc")
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        rc = rrv.main(["resolve_runner_version.py", "c"])
        assert rc == 0
        assert seen == [None]


# --- module-boundary helpers (small smoke for build_client / now_utc) ---


class TestModuleBoundaries:
    def test_now_utc_returns_aware_datetime(self):
        result = rrv.now_utc()
        assert result.tzinfo is not None

    def test_build_client_constructs_client(self):
        with patch.object(rrv, "Client") as mock_client_cls:
            rrv.build_client()
            mock_client_cls.assert_called_once_with()
