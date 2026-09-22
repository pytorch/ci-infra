"""Unit tests for the SDK config module (gpu_dev.common.config).

Target source: sdk/python/src/gpu_dev/common/config.py

These assert the REAL behavior of GpuDevConfig:
- pydantic-driven defaults
- explicit field overrides + pydantic v2 type coercion / validation
- GpuDevConfig.from_file across missing-file / bad-JSON / partial / full cases
- the default config path branch

NOTE on the task FOCUS hint ("github_user resolution incl. GPU_DEV_GITHUB_USER
fallback if present, validation"): the source has NO env-var fallback and NO
github_user resolution helper -- github_user is a plain optional pydantic field
that defaults to None and is read verbatim from the JSON file in from_file().
The tests below document/lock in that actual behavior (see
test_no_github_user_env_fallback). They do not modify the source.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from gpu_dev.common.config import GpuDevConfig, _DEFAULT_CONFIG_PATH


# --------------------------------------------------------------------------- #
# Defaults
# --------------------------------------------------------------------------- #
def test_defaults_no_args():
    c = GpuDevConfig()
    assert c.github_user is None
    assert c.environment == "prod"
    assert c.region is None
    assert c.default_timeout_minutes == 30
    assert c.poll_interval_seconds == 1.0


def test_default_field_types():
    c = GpuDevConfig()
    assert isinstance(c.default_timeout_minutes, int)
    assert isinstance(c.poll_interval_seconds, float)


# --------------------------------------------------------------------------- #
# Explicit overrides
# --------------------------------------------------------------------------- #
def test_explicit_overrides_all_fields():
    c = GpuDevConfig(
        github_user="octocat",
        environment="staging",
        region="us-west-2",
        default_timeout_minutes=60,
        poll_interval_seconds=2.5,
    )
    assert c.github_user == "octocat"
    assert c.environment == "staging"
    assert c.region == "us-west-2"
    assert c.default_timeout_minutes == 60
    assert c.poll_interval_seconds == 2.5


def test_partial_override_keeps_other_defaults():
    c = GpuDevConfig(github_user="alice")
    assert c.github_user == "alice"
    # untouched fields still hold their declared defaults
    assert c.environment == "prod"
    assert c.region is None
    assert c.default_timeout_minutes == 30
    assert c.poll_interval_seconds == 1.0


def test_github_user_explicit_none_allowed():
    c = GpuDevConfig(github_user=None)
    assert c.github_user is None


# --------------------------------------------------------------------------- #
# pydantic v2 coercion / validation
# --------------------------------------------------------------------------- #
def test_str_int_coerced_for_timeout():
    c = GpuDevConfig(default_timeout_minutes="45")
    assert c.default_timeout_minutes == 45
    assert isinstance(c.default_timeout_minutes, int)


def test_int_coerced_to_float_for_poll_interval():
    c = GpuDevConfig(poll_interval_seconds=2)
    assert c.poll_interval_seconds == 2.0
    assert isinstance(c.poll_interval_seconds, float)


def test_whole_float_coerced_to_int_for_timeout():
    # pydantic v2 accepts a float with no fractional part for an int field.
    c = GpuDevConfig(default_timeout_minutes=30.0)
    assert c.default_timeout_minutes == 30
    assert isinstance(c.default_timeout_minutes, int)


def test_fractional_float_rejected_for_int_field():
    with pytest.raises(ValidationError):
        GpuDevConfig(default_timeout_minutes=30.5)


def test_non_numeric_string_rejected_for_int_field():
    with pytest.raises(ValidationError):
        GpuDevConfig(default_timeout_minutes="not-a-number")


def test_non_numeric_string_rejected_for_float_field():
    with pytest.raises(ValidationError):
        GpuDevConfig(poll_interval_seconds="fast")


def test_unknown_field_is_ignored_not_error():
    # BaseModel default config ignores extras rather than raising.
    c = GpuDevConfig(totally_unknown_field=123)
    assert not hasattr(c, "totally_unknown_field")
    assert c.environment == "prod"


# --------------------------------------------------------------------------- #
# from_file: missing / unreadable / malformed
# --------------------------------------------------------------------------- #
def test_from_file_missing_path_returns_defaults(tmp_path):
    missing = tmp_path / "does-not-exist.json"
    c = GpuDevConfig.from_file(missing)
    assert c.github_user is None
    assert c.environment == "prod"
    assert c.region is None


def test_from_file_malformed_json_falls_back_to_defaults(tmp_path):
    p = tmp_path / "config.json"
    p.write_text("{ this is : not valid json ,,,")
    c = GpuDevConfig.from_file(p)
    assert c.github_user is None
    assert c.environment == "prod"
    assert c.region is None


def test_from_file_empty_json_object_uses_defaults(tmp_path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({}))
    c = GpuDevConfig.from_file(p)
    assert c.github_user is None
    assert c.environment == "prod"
    assert c.region is None


# --------------------------------------------------------------------------- #
# from_file: reading actual values
# --------------------------------------------------------------------------- #
def test_from_file_reads_all_three_supported_keys(tmp_path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({
        "github_user": "bob",
        "environment": "staging",
        "region": "eu-central-1",
    }))
    c = GpuDevConfig.from_file(p)
    assert c.github_user == "bob"
    assert c.environment == "staging"
    assert c.region == "eu-central-1"


def test_from_file_environment_defaults_to_prod_when_absent(tmp_path):
    # github_user present but environment omitted -> environment defaults to prod
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"github_user": "carol"}))
    c = GpuDevConfig.from_file(p)
    assert c.github_user == "carol"
    assert c.environment == "prod"
    assert c.region is None


def test_from_file_ignores_unsupported_file_keys(tmp_path):
    # from_file only forwards github_user/environment/region. Timeout and
    # poll-interval keys in the file are NOT read -> they keep model defaults.
    p = tmp_path / "config.json"
    p.write_text(json.dumps({
        "github_user": "dave",
        "environment": "staging",
        "region": "us-west-2",
        "default_timeout_minutes": 99,
        "poll_interval_seconds": 5.5,
    }))
    c = GpuDevConfig.from_file(p)
    assert c.github_user == "dave"
    assert c.environment == "staging"
    assert c.region == "us-west-2"
    # file values intentionally ignored by from_file
    assert c.default_timeout_minutes == 30
    assert c.poll_interval_seconds == 1.0


def test_from_file_null_values_in_json_preserved(tmp_path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({
        "github_user": None,
        "environment": None,
        "region": None,
    }))
    # environment is None in the file -> data.get("environment", "prod") returns
    # None (key present), and pydantic rejects None for the non-optional str field.
    with pytest.raises(ValidationError):
        GpuDevConfig.from_file(p)


# --------------------------------------------------------------------------- #
# Default path branch
# --------------------------------------------------------------------------- #
def test_default_config_path_is_user_config_dir():
    assert _DEFAULT_CONFIG_PATH == Path.home() / ".config" / "gpu-dev" / "config.json"


def test_from_file_uses_default_path_when_none(monkeypatch, tmp_path):
    # Patch the module-level default path to a controlled file and call with no arg.
    target = tmp_path / "default-config.json"
    target.write_text(json.dumps({"github_user": "erin", "environment": "staging"}))
    monkeypatch.setattr(
        "gpu_dev.common.config._DEFAULT_CONFIG_PATH", target, raising=True
    )
    c = GpuDevConfig.from_file()
    assert c.github_user == "erin"
    assert c.environment == "staging"


def test_from_file_default_path_missing_returns_defaults(monkeypatch, tmp_path):
    target = tmp_path / "absent.json"  # never created
    monkeypatch.setattr(
        "gpu_dev.common.config._DEFAULT_CONFIG_PATH", target, raising=True
    )
    c = GpuDevConfig.from_file()
    assert c.github_user is None
    assert c.environment == "prod"
    assert c.region is None


# --------------------------------------------------------------------------- #
# Documenting the absence of the hinted env fallback
# --------------------------------------------------------------------------- #
def test_no_github_user_env_fallback(monkeypatch):
    # The FOCUS hint mentions a GPU_DEV_GITHUB_USER env fallback. The source has
    # no such behavior: constructing with no args ignores the env var entirely.
    monkeypatch.setenv("GPU_DEV_GITHUB_USER", "from-env-should-be-ignored")
    c = GpuDevConfig()
    assert c.github_user is None


def test_no_github_user_env_fallback_in_from_file(monkeypatch, tmp_path):
    monkeypatch.setenv("GPU_DEV_GITHUB_USER", "from-env-should-be-ignored")
    p = tmp_path / "config.json"
    p.write_text(json.dumps({}))
    c = GpuDevConfig.from_file(p)
    assert c.github_user is None
