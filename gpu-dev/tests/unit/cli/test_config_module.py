"""Unit tests for the Config class in gpu_dev_cli.config.

Source under test: cli-tools/gpu-dev-cli/gpu_dev_cli/config.py

Focus areas:
  - ENVIRONMENTS table contents + DEFAULT_ENVIRONMENT
  - region resolution priority (AWS_* env > GPU_DEV_ENVIRONMENT override >
    persisted config > env's region > hardcoded fallback)
  - environment/prefix -> derived resource names (queue/table/cluster)
  - config load: file, legacy migration, default-filling + save-on-defaults
  - save_config / get / set_environment
  - get_github_username fallback to GPU_DEV_GITHUB_USER

Config.__init__ touches the filesystem (config + cred cache) and AWS (boto3
session). We isolate BOTH: every Config instance is built with class-level
Path attributes redirected into a tmp dir, and _create_aws_session stubbed to a
MagicMock. A `make_config` factory centralizes that wiring. AWS-region env vars
from the root conftest are cleared per test so region-resolution branches are
deterministic.
"""
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from gpu_dev_cli import config as config_mod
from gpu_dev_cli.config import Config, load_config


# --------------------------------------------------------------------------
# Helpers / fixtures
# --------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _clear_aws_region_env(monkeypatch):
    """The root conftest sets AWS_REGION/AWS_DEFAULT_REGION=us-east-2 globally.
    Clear them so each test controls region resolution explicitly, and drop the
    env-override knobs that would otherwise leak between tests.
    """
    for k in ("AWS_REGION", "AWS_DEFAULT_REGION", "GPU_DEV_ENVIRONMENT",
              "GPU_DEV_GITHUB_USER"):
        monkeypatch.delenv(k, raising=False)


@pytest.fixture
def cfg_paths(tmp_path, monkeypatch):
    """Redirect all Config class-level Path attributes into an isolated tmp dir.

    Returns a small namespace with the redirected paths so tests can pre-seed
    a config file / legacy files before constructing a Config.
    """
    base = tmp_path / "cfgroot"
    main = base / ".config" / "gpu-dev" / "config.json"
    legacy_cfg = base / ".gpu-dev-config"
    legacy_env = base / ".gpu-dev-environment.json"
    cred = base / ".config" / "gpu-dev" / "aws-cred-cache.json"

    monkeypatch.setattr(Config, "CONFIG_FILE", main)
    monkeypatch.setattr(Config, "LEGACY_CONFIG_FILE", legacy_cfg)
    monkeypatch.setattr(Config, "LEGACY_ENVIRONMENT_FILE", legacy_env)
    monkeypatch.setattr(Config, "_CRED_CACHE", cred)

    class _Paths:
        pass

    p = _Paths()
    p.main = main
    p.legacy_cfg = legacy_cfg
    p.legacy_env = legacy_env
    p.cred = cred
    return p


@pytest.fixture
def make_config(cfg_paths):
    """Factory building a Config with a stubbed AWS session.

    Keeps __init__ fully offline. Returns the constructed Config.
    """
    def _make():
        fake_session = MagicMock(name="session")
        with patch.object(Config, "_create_aws_session", return_value=fake_session):
            return Config()
    _make.paths = cfg_paths
    return _make


def _write_json(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


# --------------------------------------------------------------------------
# ENVIRONMENTS / class-level constants
# --------------------------------------------------------------------------
def test_default_environment_is_prod():
    assert Config.DEFAULT_ENVIRONMENT == "prod"
    assert Config.DEFAULT_ENVIRONMENT in Config.ENVIRONMENTS


def test_environments_have_expected_regions():
    assert Config.ENVIRONMENTS["test"]["region"] == "us-west-1"
    assert Config.ENVIRONMENTS["prod"]["region"] == "us-east-2"
    assert Config.ENVIRONMENTS["prod-east1"]["region"] == "us-east-1"
    # staging lives in us-west-1 (tf workspace "test"); the earlier us-west-2 /
    # staging-west2 guess was corrected in commit cbaef6d after live validation.
    assert Config.ENVIRONMENTS["staging"]["region"] == "us-west-1"


def test_staging_uses_standard_prefix():
    # What matters functionally: staging has NO custom prefix override, so it gets
    # the same standard `pytorch-gpu-dev` prefix as prod and only the region differs.
    # (The workspace string is informational metadata, defined once in ENVIRONMENTS;
    # we don't re-assert the literal here — that'd just duplicate the constant.)
    assert "prefix" not in Config.ENVIRONMENTS["staging"]
    assert "prefix" not in Config.ENVIRONMENTS["prod"]


def test_prod_east1_lists_spot_types():
    spot = Config.ENVIRONMENTS["prod-east1"]["spot_types"]
    assert "b300" in spot and "h100" in spot and "t4" in spot


# --------------------------------------------------------------------------
# region resolution priority
# --------------------------------------------------------------------------
def test_default_region_is_prod_when_no_config_no_env(make_config):
    # Fresh config dir -> _load_config fills region from prod default (us-east-2).
    cfg = make_config()
    assert cfg.aws_region == "us-east-2"


def test_aws_region_env_wins_over_persisted(make_config, monkeypatch):
    # Persisted config says us-west-1, but AWS_REGION overrides it.
    _write_json(make_config.paths.main, {"region": "us-west-1",
                                         "environment": "test",
                                         "workspace": "default"})
    monkeypatch.setenv("AWS_REGION", "eu-central-1")
    cfg = make_config()
    assert cfg.aws_region == "eu-central-1"


def test_aws_default_region_used_when_aws_region_absent(make_config, monkeypatch):
    _write_json(make_config.paths.main, {"region": "us-west-1",
                                         "environment": "test",
                                         "workspace": "default"})
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-south-1")
    cfg = make_config()
    assert cfg.aws_region == "ap-south-1"


def test_aws_region_equal_to_persisted_does_not_count_as_override(make_config, monkeypatch):
    # The branch is `if env_region and env_region != persisted`. When they match,
    # it falls through to the persisted-config branch (same value here).
    _write_json(make_config.paths.main, {"region": "us-west-1",
                                         "environment": "test",
                                         "workspace": "default"})
    monkeypatch.setenv("AWS_REGION", "us-west-1")
    cfg = make_config()
    assert cfg.aws_region == "us-west-1"


def test_environment_override_region_beats_persisted(make_config, monkeypatch):
    # Persisted region is us-east-2 (prod), but GPU_DEV_ENVIRONMENT=staging
    # forces the staging region (no AWS_* env set). Staging is us-west-1.
    _write_json(make_config.paths.main, {"region": "us-east-2",
                                         "environment": "prod",
                                         "workspace": "prod"})
    monkeypatch.setenv("GPU_DEV_ENVIRONMENT", "staging")
    cfg = make_config()
    assert cfg.aws_region == "us-west-1"


def test_persisted_region_used_when_no_env(make_config):
    _write_json(make_config.paths.main, {"region": "us-west-1",
                                         "environment": "test",
                                         "workspace": "default"})
    cfg = make_config()
    assert cfg.aws_region == "us-west-1"


def test_unknown_environment_override_falls_back_to_persisted_region(make_config, monkeypatch):
    # env_cfg = {} for an unknown env name, so env_cfg.get("region") is falsy and
    # the env-override branch is skipped; persisted region is used.
    _write_json(make_config.paths.main, {"region": "us-west-1",
                                         "environment": "prod",
                                         "workspace": "prod"})
    monkeypatch.setenv("GPU_DEV_ENVIRONMENT", "does-not-exist")
    cfg = make_config()
    assert cfg.aws_region == "us-west-1"


def test_aws_default_region_env_is_persisted_to_os_environ(make_config):
    cfg = make_config()
    import os
    assert os.environ["AWS_DEFAULT_REGION"] == cfg.aws_region


# --------------------------------------------------------------------------
# prefix + derived resource names
# --------------------------------------------------------------------------
def test_default_prefix_and_resource_names(make_config):
    cfg = make_config()
    assert cfg.prefix == "pytorch-gpu-dev"
    assert cfg.queue_name == "pytorch-gpu-dev-reservation-queue"
    assert cfg.reservations_table == "pytorch-gpu-dev-reservations"
    assert cfg.disks_table == "pytorch-gpu-dev-disks"
    assert cfg.operations_table == "pytorch-gpu-dev-operations"
    assert cfg.availability_table == "pytorch-gpu-dev-gpu-availability"
    assert cfg.cluster_name == "pytorch-gpu-dev-cluster"


def test_staging_environment_uses_standard_prefix(make_config, monkeypatch):
    # Staging shares prod's standard resource prefix; only the region differs.
    monkeypatch.setenv("GPU_DEV_ENVIRONMENT", "staging")
    cfg = make_config()
    assert cfg.prefix == "pytorch-gpu-dev"
    assert cfg.queue_name == "pytorch-gpu-dev-reservation-queue"
    assert cfg.cluster_name == "pytorch-gpu-dev-cluster"


# --------------------------------------------------------------------------
# _load_config: file present / defaults / migration
# --------------------------------------------------------------------------
def test_load_existing_config_file(make_config):
    _write_json(make_config.paths.main, {"region": "us-west-1",
                                         "environment": "test",
                                         "workspace": "default",
                                         "github_user": "carol"})
    cfg = make_config()
    assert cfg.user_config["github_user"] == "carol"
    assert cfg.user_config["environment"] == "test"


def test_missing_config_fills_defaults_and_writes_file(make_config):
    assert not make_config.paths.main.exists()
    cfg = make_config()
    # defaults filled in-memory
    assert cfg.user_config["environment"] == "prod"
    assert cfg.user_config["region"] == "us-east-2"
    assert cfg.user_config["workspace"] == "prod"
    # and persisted to disk
    assert make_config.paths.main.exists()
    saved = json.loads(make_config.paths.main.read_text())
    assert saved["environment"] == "prod"
    assert saved["region"] == "us-east-2"
    assert saved["workspace"] == "prod"


def test_partial_config_gets_missing_keys_filled(make_config):
    # Only region present -> environment + workspace get defaulted.
    _write_json(make_config.paths.main, {"region": "us-west-1"})
    cfg = make_config()
    assert cfg.user_config["region"] == "us-west-1"  # preserved
    assert cfg.user_config["environment"] == "prod"  # filled
    assert cfg.user_config["workspace"] == "prod"    # filled


def test_migration_from_legacy_config_files(make_config):
    # No new config file; both legacy files present -> merged + migrated.
    _write_json(make_config.paths.legacy_cfg, {"github_user": "dave"})
    _write_json(make_config.paths.legacy_env, {"environment": "test",
                                               "region": "us-west-1",
                                               "workspace": "default"})
    cfg = make_config()
    assert cfg.user_config["github_user"] == "dave"
    assert cfg.user_config["environment"] == "test"
    # migration writes the unified file
    assert make_config.paths.main.exists()
    saved = json.loads(make_config.paths.main.read_text())
    assert saved["github_user"] == "dave"


def test_corrupt_config_file_is_handled_and_defaults_used(make_config):
    make_config.paths.main.parent.mkdir(parents=True, exist_ok=True)
    make_config.paths.main.write_text("{ this is not json")
    cfg = make_config()
    # load failed -> empty config -> defaults filled
    assert cfg.user_config["environment"] == "prod"
    assert cfg.user_config["region"] == "us-east-2"


def test_existing_complete_config_not_rewritten(make_config):
    # A complete config (all required keys) shouldn't trigger a needs_save write.
    full = {"region": "us-west-1", "environment": "test", "workspace": "default"}
    _write_json(make_config.paths.main, full)
    before = make_config.paths.main.read_text()
    make_config()
    after = make_config.paths.main.read_text()
    assert before == after  # untouched (no reserialization)


# --------------------------------------------------------------------------
# save_config / get
# --------------------------------------------------------------------------
def test_save_config_updates_memory_and_disk(make_config):
    cfg = make_config()
    cfg.save_config("github_user", "erin")
    assert cfg.user_config["github_user"] == "erin"
    saved = json.loads(make_config.paths.main.read_text())
    assert saved["github_user"] == "erin"


def test_get_returns_value_and_none_for_missing(make_config):
    cfg = make_config()
    cfg.save_config("github_user", "frank")
    assert cfg.get("github_user") == "frank"
    assert cfg.get("nonexistent_key") is None


# --------------------------------------------------------------------------
# set_environment
# --------------------------------------------------------------------------
def test_set_environment_valid_updates_region_and_persists(make_config):
    cfg = make_config()
    ret = cfg.set_environment("test")
    assert ret == Config.ENVIRONMENTS["test"]
    assert cfg.aws_region == "us-west-1"
    assert cfg.user_config["environment"] == "test"
    assert cfg.user_config["region"] == "us-west-1"
    assert cfg.user_config["workspace"] == "default"
    saved = json.loads(make_config.paths.main.read_text())
    assert saved["environment"] == "test"
    import os
    assert os.environ["AWS_DEFAULT_REGION"] == "us-west-1"


def test_set_environment_invalid_raises_valueerror(make_config):
    cfg = make_config()
    with pytest.raises(ValueError) as exc:
        cfg.set_environment("nope")
    assert "Invalid environment" in str(exc.value)
    assert "nope" in str(exc.value)
    # valid keys listed in the message
    assert "prod" in str(exc.value)


def test_set_environment_to_staging_sets_west1(make_config):
    cfg = make_config()
    cfg.set_environment("staging")
    assert cfg.aws_region == "us-west-1"
    # Behavioral: set_environment copies workspace from the single source
    # (ENVIRONMENTS) — assert against the source, never a hardcoded literal.
    assert cfg.user_config["workspace"] == Config.ENVIRONMENTS["staging"]["workspace"]


# --------------------------------------------------------------------------
# get_github_username fallback
# --------------------------------------------------------------------------
def test_github_username_from_config(make_config, monkeypatch):
    _write_json(make_config.paths.main, {"region": "us-east-2",
                                         "environment": "prod",
                                         "workspace": "prod",
                                         "github_user": "grace"})
    monkeypatch.setenv("GPU_DEV_GITHUB_USER", "env-user")
    cfg = make_config()
    # config value wins over env var
    assert cfg.get_github_username() == "grace"


def test_github_username_falls_back_to_env(make_config, monkeypatch):
    cfg = make_config()  # no github_user in config
    monkeypatch.setenv("GPU_DEV_GITHUB_USER", "poduser")
    assert cfg.get_github_username() == "poduser"


def test_github_username_none_when_neither_set(make_config):
    cfg = make_config()
    assert cfg.get_github_username() is None


def test_github_username_empty_config_value_falls_through_to_env(make_config, monkeypatch):
    # Empty string in config is falsy -> falls to env var.
    _write_json(make_config.paths.main, {"region": "us-east-2",
                                         "environment": "prod",
                                         "workspace": "prod",
                                         "github_user": ""})
    monkeypatch.setenv("GPU_DEV_GITHUB_USER", "fallback")
    cfg = make_config()
    assert cfg.get_github_username() == "fallback"


def test_github_username_empty_env_returns_none(make_config, monkeypatch):
    # Empty env var -> `v or None` yields None.
    cfg = make_config()
    monkeypatch.setenv("GPU_DEV_GITHUB_USER", "")
    assert cfg.get_github_username() is None


# --------------------------------------------------------------------------
# lazy AWS clients (no real boto)
# --------------------------------------------------------------------------
def test_clients_are_lazy_and_cached(make_config):
    cfg = make_config()
    # session is the MagicMock from the factory; clients should be lazily built.
    assert cfg._sts_client is None
    first = cfg.sts_client
    second = cfg.sts_client
    assert first is second  # cached
    cfg.session.client.assert_any_call("sts", region_name=cfg.aws_region)


def test_dynamodb_uses_resource_not_client(make_config):
    cfg = make_config()
    assert cfg._dynamodb is None
    _ = cfg.dynamodb
    cfg.session.resource.assert_called_once_with("dynamodb", region_name=cfg.aws_region)


def test_refresh_session_clears_clients_and_recreates(make_config):
    cfg = make_config()
    _ = cfg.sts_client  # populate cache
    assert cfg._sts_client is not None
    new_session = MagicMock(name="new_session")
    with patch.object(Config, "_create_aws_session", return_value=new_session):
        cfg.refresh_session()
    assert cfg._sts_client is None
    assert cfg._sqs_client is None
    assert cfg._dynamodb is None
    assert cfg.session is new_session


# --------------------------------------------------------------------------
# get_queue_url / get_user_identity error wrapping
# --------------------------------------------------------------------------
def test_get_queue_url_success(make_config):
    cfg = make_config()
    cfg._sqs_client = MagicMock()
    cfg._sqs_client.get_queue_url.return_value = {"QueueUrl": "https://q/url"}
    assert cfg.get_queue_url() == "https://q/url"
    cfg._sqs_client.get_queue_url.assert_called_once_with(QueueName=cfg.queue_name)


def test_get_queue_url_wraps_error(make_config):
    cfg = make_config()
    cfg._sqs_client = MagicMock()
    cfg._sqs_client.get_queue_url.side_effect = Exception("denied")
    with pytest.raises(RuntimeError) as exc:
        cfg.get_queue_url()
    assert "Cannot access SQS queue" in str(exc.value)
    assert cfg.queue_name in str(exc.value)


def test_get_user_identity_success(make_config):
    cfg = make_config()
    cfg._sts_client = MagicMock()
    cfg._sts_client.get_caller_identity.return_value = {
        "UserId": "AIDA", "Account": "123456789012",
        "Arn": "arn:aws:iam::123456789012:user/bob",
    }
    out = cfg.get_user_identity()
    assert out == {"user_id": "AIDA", "account": "123456789012",
                   "arn": "arn:aws:iam::123456789012:user/bob"}


def test_get_user_identity_wraps_error(make_config):
    cfg = make_config()
    cfg._sts_client = MagicMock()
    cfg._sts_client.get_caller_identity.side_effect = Exception("no creds")
    with pytest.raises(RuntimeError) as exc:
        cfg.get_user_identity()
    assert "Cannot get AWS caller identity" in str(exc.value)


# --------------------------------------------------------------------------
# load_config module function
# --------------------------------------------------------------------------
def test_load_config_returns_config_instance(cfg_paths):
    with patch.object(Config, "_create_aws_session", return_value=MagicMock()):
        cfg = load_config()
    assert isinstance(cfg, Config)


def test_module_exposes_config_and_load_config():
    assert config_mod.Config is Config
    assert callable(config_mod.load_config)
