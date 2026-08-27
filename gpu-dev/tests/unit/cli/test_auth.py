"""Unit tests for gpu_dev_cli.auth.

Covers authenticate_user (missing github_user -> RuntimeError, ARN -> user_id
derivation, the auth cache short-circuit, save-on-success, and the
clear-cache-then-RuntimeError failure path) plus the auth-cache helpers
(_load_auth_cache TTL / github_user mismatch / AWS_ROLE_ARN defense,
_save_auth_cache roundtrip, clear_auth_cache) and
validate_ssh_key_matches_github_user (missing user, cache hit, success parse,
case-insensitive match/mismatch, unparseable output, timeout, host-key failure,
generic SSH error, and the valid-only ssh cache save).

Everything that touches the network/disk/STS is mocked. The module-level cache
PATHS are redirected to tmp files in *every* test via the `tmp_caches` fixture
so the real ~/.config/gpu-dev caches are never read or written.
"""
import json
import subprocess
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from gpu_dev_cli import auth


@pytest.fixture(autouse=True)
def tmp_caches(tmp_path, monkeypatch):
    """Redirect both on-disk caches to tmp files for the duration of each test."""
    auth_path = tmp_path / "auth-cache.json"
    ssh_path = tmp_path / "ssh-validation-cache.json"
    monkeypatch.setattr(auth, "_AUTH_CACHE_PATH", auth_path)
    monkeypatch.setattr(auth, "_SSH_CACHE_PATH", ssh_path)
    # Stable, deterministic auth-cache key regardless of the host's AWS_PROFILE.
    monkeypatch.setenv("AWS_PROFILE", "unittest-profile")
    # No AWS_ROLE_ARN by default (the role-name defense is opt-in by env).
    monkeypatch.delenv("AWS_ROLE_ARN", raising=False)
    return {"auth": auth_path, "ssh": ssh_path}


def _make_config(github_user="octocat", arn=None, identity_raises=None):
    cfg = MagicMock(name="Config")
    cfg.get_github_username.return_value = github_user
    if identity_raises is not None:
        cfg.get_user_identity.side_effect = identity_raises
    else:
        if arn is None:
            arn = "arn:aws:iam::123456789012:user/octocat"
        cfg.get_user_identity.return_value = {
            "user_id": "AIDXXXXXXX",
            "account": "123456789012",
            "arn": arn,
        }
    return cfg


# --------------------------------------------------------------------------- #
# authenticate_user
# --------------------------------------------------------------------------- #
def test_authenticate_user_missing_github_user_raises():
    cfg = _make_config(github_user=None)
    with pytest.raises(RuntimeError) as exc:
        auth.authenticate_user(cfg)
    assert "GitHub username not configured" in str(exc.value)
    assert "gpu-dev config set github_user" in str(exc.value)
    # Never reached STS.
    cfg.get_user_identity.assert_not_called()


def test_authenticate_user_empty_github_user_raises():
    cfg = _make_config(github_user="")
    with pytest.raises(RuntimeError):
        auth.authenticate_user(cfg)


def test_authenticate_user_derives_user_id_from_arn():
    cfg = _make_config(arn="arn:aws:sts::123456789012:assumed-role/MyRole/wouter")
    result = auth.authenticate_user(cfg)
    assert result == {
        "user_id": "wouter",
        "github_user": "octocat",
        "arn": "arn:aws:sts::123456789012:assumed-role/MyRole/wouter",
    }
    cfg.get_user_identity.assert_called_once()


def test_authenticate_user_iam_user_arn_user_id():
    cfg = _make_config(arn="arn:aws:iam::111122223333:user/alice")
    result = auth.authenticate_user(cfg)
    assert result["user_id"] == "alice"
    assert result["arn"].endswith("/alice")


def test_authenticate_user_saves_cache_on_success(tmp_caches):
    cfg = _make_config(arn="arn:aws:iam::111:user/bob")
    auth.authenticate_user(cfg)
    data = json.loads(Path(tmp_caches["auth"]).read_text())
    entry = data["unittest-profile"]
    assert entry["github_user"] == "octocat"
    assert entry["result"]["user_id"] == "bob"
    assert "ts" in entry


def test_authenticate_user_uses_cache_and_skips_sts():
    cfg = _make_config(arn="arn:aws:iam::111:user/first")
    first = auth.authenticate_user(cfg)
    assert cfg.get_user_identity.call_count == 1

    # Second call with a *different* ARN should return the cached value and NOT
    # re-hit STS.
    cfg.get_user_identity.return_value = {
        "arn": "arn:aws:iam::111:user/CHANGED",
        "user_id": "x",
        "account": "111",
    }
    second = auth.authenticate_user(cfg)
    assert second == first
    assert second["user_id"] == "first"
    assert cfg.get_user_identity.call_count == 1  # cache short-circuit


def test_authenticate_user_failure_clears_cache_and_raises():
    cfg = _make_config(identity_raises=RuntimeError("creds expired"))
    with pytest.raises(RuntimeError) as exc:
        auth.authenticate_user(cfg)
    assert "AWS authentication failed" in str(exc.value)
    assert "creds expired" in str(exc.value)


def test_authenticate_user_failure_after_cached_success_clears_cache(tmp_caches):
    # Prime a good cache, then break STS and corrupt cache so it's a miss, and
    # verify the failure path clears the (now-missing) entry without error.
    good = _make_config(arn="arn:aws:iam::111:user/bob")
    auth.authenticate_user(good)
    assert Path(tmp_caches["auth"]).exists()

    # New profile key so the existing cache entry doesn't satisfy this call.
    import os
    os.environ["AWS_PROFILE"] = "other-profile"
    bad = _make_config(identity_raises=Exception("boom"))
    with pytest.raises(RuntimeError):
        auth.authenticate_user(bad)
    os.environ["AWS_PROFILE"] = "unittest-profile"


# --------------------------------------------------------------------------- #
# _load_auth_cache / _save_auth_cache / clear_auth_cache
# --------------------------------------------------------------------------- #
def test_load_auth_cache_missing_file_returns_none():
    assert auth._load_auth_cache("octocat") is None


def test_save_then_load_auth_cache_roundtrip():
    result = {"user_id": "bob", "github_user": "octocat", "arn": "a/bob"}
    auth._save_auth_cache("octocat", result)
    assert auth._load_auth_cache("octocat") == result


def test_load_auth_cache_github_user_mismatch_returns_none():
    auth._save_auth_cache("octocat", {"arn": "a/bob"})
    assert auth._load_auth_cache("someone-else") is None


def test_load_auth_cache_expired_ttl_returns_none(tmp_caches, monkeypatch):
    # Write an entry whose timestamp is older than the TTL.
    stale_ts = int(time.time()) - auth._AUTH_CACHE_TTL_SECONDS - 10
    data = {
        "unittest-profile": {
            "github_user": "octocat",
            "ts": stale_ts,
            "result": {"arn": "a/bob"},
        }
    }
    Path(tmp_caches["auth"]).write_text(json.dumps(data))
    assert auth._load_auth_cache("octocat") is None


def test_load_auth_cache_fresh_within_ttl_returns_result(tmp_caches):
    fresh_ts = int(time.time())
    data = {
        "unittest-profile": {
            "github_user": "octocat",
            "ts": fresh_ts,
            "result": {"arn": "a/bob", "user_id": "bob"},
        }
    }
    Path(tmp_caches["auth"]).write_text(json.dumps(data))
    assert auth._load_auth_cache("octocat") == {"arn": "a/bob", "user_id": "bob"}


def test_load_auth_cache_role_arn_mismatch_returns_none(tmp_caches, monkeypatch):
    # AWS_ROLE_ARN role name not present in cached arn -> reject as stale identity.
    monkeypatch.setenv("AWS_ROLE_ARN", "arn:aws:iam::111:role/NewRole")
    data = {
        "unittest-profile": {
            "github_user": "octocat",
            "ts": int(time.time()),
            "result": {"arn": "arn:aws:sts::111:assumed-role/OldRole/bob"},
        }
    }
    Path(tmp_caches["auth"]).write_text(json.dumps(data))
    assert auth._load_auth_cache("octocat") is None


def test_load_auth_cache_role_arn_match_returns_result(tmp_caches, monkeypatch):
    monkeypatch.setenv("AWS_ROLE_ARN", "arn:aws:iam::111:role/MyRole")
    data = {
        "unittest-profile": {
            "github_user": "octocat",
            "ts": int(time.time()),
            "result": {"arn": "arn:aws:sts::111:assumed-role/MyRole/bob"},
        }
    }
    Path(tmp_caches["auth"]).write_text(json.dumps(data))
    assert auth._load_auth_cache("octocat") == {
        "arn": "arn:aws:sts::111:assumed-role/MyRole/bob"
    }


def test_load_auth_cache_corrupt_json_returns_none(tmp_caches):
    Path(tmp_caches["auth"]).write_text("{not valid json")
    assert auth._load_auth_cache("octocat") is None


def test_save_auth_cache_preserves_other_profiles(tmp_caches, monkeypatch):
    # Pre-populate a different profile's entry; saving ours must not drop it.
    Path(tmp_caches["auth"]).write_text(
        json.dumps({"prod": {"github_user": "x", "ts": 1, "result": {"arn": "a/x"}}})
    )
    auth._save_auth_cache("octocat", {"arn": "a/bob"})
    data = json.loads(Path(tmp_caches["auth"]).read_text())
    assert "prod" in data
    assert "unittest-profile" in data


def test_clear_auth_cache_removes_current_profile_only(tmp_caches):
    Path(tmp_caches["auth"]).write_text(
        json.dumps({
            "unittest-profile": {"github_user": "octocat", "ts": 1, "result": {}},
            "prod": {"github_user": "x", "ts": 1, "result": {}},
        })
    )
    auth.clear_auth_cache()
    data = json.loads(Path(tmp_caches["auth"]).read_text())
    assert "unittest-profile" not in data
    assert "prod" in data


def test_clear_auth_cache_no_file_is_noop():
    # Should not raise when there's no cache file.
    auth.clear_auth_cache()


# --------------------------------------------------------------------------- #
# validate_ssh_key_matches_github_user
# --------------------------------------------------------------------------- #
def test_validate_ssh_missing_github_user():
    cfg = _make_config(github_user=None)
    result = auth.validate_ssh_key_matches_github_user(cfg)
    assert result["valid"] is False
    assert result["configured_user"] is None
    assert result["ssh_user"] is None
    assert "GitHub username not configured" in result["error"]


def test_validate_ssh_uses_cache_when_present(tmp_caches):
    cached = {
        "valid": True,
        "configured_user": "octocat",
        "ssh_user": "octocat",
        "error": None,
    }
    Path(tmp_caches["ssh"]).write_text(
        json.dumps({"configured_user": "octocat", "ts": int(time.time()), "result": cached})
    )
    cfg = _make_config(github_user="octocat")
    result = auth.validate_ssh_key_matches_github_user(cfg)
    assert result == cached


def test_validate_ssh_success_parses_and_caches(monkeypatch, tmp_caches):
    completed = MagicMock(returncode=1)

    def fake_run(cmd, **kwargs):
        # Simulate GitHub's banner being written to the stderr temp file.
        stderr_file = kwargs["stderr"]
        stderr_file.write(
            "Hi octocat! You've successfully authenticated, "
            "but GitHub does not provide shell access.\n"
        )
        stderr_file.flush()
        stderr_file.seek(0)
        return completed

    monkeypatch.setattr(auth.subprocess, "run", fake_run)
    cfg = _make_config(github_user="octocat")
    result = auth.validate_ssh_key_matches_github_user(cfg)
    assert result["valid"] is True
    assert result["ssh_user"] == "octocat"
    assert result["configured_user"] == "octocat"
    assert result["error"] is None
    # Valid result is persisted.
    saved = json.loads(Path(tmp_caches["ssh"]).read_text())
    assert saved["result"]["valid"] is True
    assert saved["configured_user"] == "octocat"


def test_validate_ssh_case_insensitive_match(monkeypatch):
    def fake_run(cmd, **kwargs):
        f = kwargs["stderr"]
        f.write("Hi OctoCat! You've successfully authenticated, blah\n")
        f.flush()
        f.seek(0)
        return MagicMock(returncode=1)

    monkeypatch.setattr(auth.subprocess, "run", fake_run)
    cfg = _make_config(github_user="octocat")
    result = auth.validate_ssh_key_matches_github_user(cfg)
    assert result["valid"] is True
    assert result["ssh_user"] == "OctoCat"


def test_validate_ssh_username_mismatch(monkeypatch, tmp_caches):
    def fake_run(cmd, **kwargs):
        f = kwargs["stderr"]
        f.write("Hi someone-else! You've successfully authenticated, blah\n")
        f.flush()
        f.seek(0)
        return MagicMock(returncode=1)

    monkeypatch.setattr(auth.subprocess, "run", fake_run)
    cfg = _make_config(github_user="octocat")
    result = auth.validate_ssh_key_matches_github_user(cfg)
    assert result["valid"] is False
    assert result["ssh_user"] == "someone-else"
    assert "someone-else" in result["error"]
    assert "octocat" in result["error"]
    # Invalid result must NOT be cached.
    assert not Path(tmp_caches["ssh"]).exists()


def test_validate_ssh_unparseable_output(monkeypatch):
    def fake_run(cmd, **kwargs):
        f = kwargs["stderr"]
        f.write("Permission denied (publickey).\n")
        f.flush()
        f.seek(0)
        return MagicMock(returncode=255)

    monkeypatch.setattr(auth.subprocess, "run", fake_run)
    cfg = _make_config(github_user="octocat")
    result = auth.validate_ssh_key_matches_github_user(cfg)
    assert result["valid"] is False
    assert result["ssh_user"] is None
    assert "Could not parse GitHub SSH response" in result["error"]


def test_validate_ssh_timeout(monkeypatch):
    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd="ssh", timeout=30)

    monkeypatch.setattr(auth.subprocess, "run", fake_run)
    cfg = _make_config(github_user="octocat")
    result = auth.validate_ssh_key_matches_github_user(cfg)
    assert result["valid"] is False
    assert result["ssh_user"] is None
    assert "timed out" in result["error"]


def test_validate_ssh_host_key_verification_failed(monkeypatch):
    def fake_run(cmd, **kwargs):
        f = kwargs["stderr"]
        f.write("Host key verification failed.\n")
        f.flush()
        f.seek(0)
        return MagicMock(returncode=255)

    monkeypatch.setattr(auth.subprocess, "run", fake_run)
    cfg = _make_config(github_user="octocat")
    result = auth.validate_ssh_key_matches_github_user(cfg)
    # The CalledProcessError raised inside the inner try is caught by the inner
    # generic handler -> "SSH connection failed".
    assert result["valid"] is False
    assert "SSH connection failed" in result["error"]


def test_validate_ssh_generic_subprocess_error(monkeypatch):
    def fake_run(cmd, **kwargs):
        raise OSError("ssh binary not found")

    monkeypatch.setattr(auth.subprocess, "run", fake_run)
    cfg = _make_config(github_user="octocat")
    result = auth.validate_ssh_key_matches_github_user(cfg)
    assert result["valid"] is False
    assert "SSH connection failed" in result["error"]
    assert "ssh binary not found" in result["error"]


def test_validate_ssh_live_spinner_stopped_and_restarted(monkeypatch):
    live = MagicMock()

    def fake_run(cmd, **kwargs):
        f = kwargs["stderr"]
        f.write("Hi octocat! You've successfully authenticated, blah\n")
        f.flush()
        f.seek(0)
        return MagicMock(returncode=1)

    monkeypatch.setattr(auth.subprocess, "run", fake_run)
    cfg = _make_config(github_user="octocat")
    result = auth.validate_ssh_key_matches_github_user(cfg, live=live)
    assert result["valid"] is True
    live.stop.assert_called_once()
    live.start.assert_called_once()


def test_save_ssh_cache_skips_invalid(tmp_caches):
    auth._save_ssh_cache("octocat", {"valid": False, "error": "nope"})
    assert not Path(tmp_caches["ssh"]).exists()


def test_save_ssh_cache_persists_valid(tmp_caches):
    auth._save_ssh_cache("octocat", {"valid": True, "ssh_user": "octocat"})
    assert Path(tmp_caches["ssh"]).exists()
    saved = json.loads(Path(tmp_caches["ssh"]).read_text())
    assert saved["configured_user"] == "octocat"
    assert saved["result"]["valid"] is True


def test_load_ssh_cache_user_mismatch_returns_none(tmp_caches):
    Path(tmp_caches["ssh"]).write_text(
        json.dumps({"configured_user": "other", "ts": int(time.time()), "result": {"valid": True}})
    )
    assert auth._load_ssh_cache("octocat") is None


def test_load_ssh_cache_expired_returns_none(tmp_caches):
    stale = int(time.time()) - auth._SSH_CACHE_TTL_SECONDS - 10
    Path(tmp_caches["ssh"]).write_text(
        json.dumps({"configured_user": "octocat", "ts": stale, "result": {"valid": True}})
    )
    assert auth._load_ssh_cache("octocat") is None
