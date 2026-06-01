"""Unit tests for the `gpu-dev config` command group (config show / config set).

Source under test: cli-tools/gpu-dev-cli/gpu_dev_cli/cli.py

The `config` group has:
  - show: renders a Rich panel of AWS identity + user settings (read-only).
  - set KEY VALUE: validates KEY against a whitelist (`github_user`) and calls
    Config.save_config; unknown keys are rejected without saving.

`load_config` is imported into the cli module namespace (`from .config import
... load_config`), so we patch it at `gpu_dev_cli.cli.load_config`. A MagicMock
Config lets us assert exactly which methods get called and avoids any file I/O
or AWS access.
"""
from unittest.mock import MagicMock, patch

import pytest

from gpu_dev_cli.cli import main


def _config_mock(github_user="bobthebuilder", identity=None, user_cfg=None):
    """A fully-stubbed Config double for the show/set commands."""
    cfg = MagicMock(name="Config")
    cfg.get_user_identity.return_value = identity or {
        "arn": "arn:aws:iam::123456789012:user/bob",
        "account": "123456789012",
    }
    cfg.get_github_username.return_value = github_user
    _uc = user_cfg or {"environment": "prod", "region": "us-east-2"}
    cfg.get.side_effect = lambda k: _uc.get(k)
    cfg.aws_region = "us-east-2"
    cfg.queue_name = "pytorch-gpu-dev-reservation-queue"
    cfg.cluster_name = "pytorch-gpu-dev-cluster"
    cfg.CONFIG_FILE = "/home/bob/.config/gpu-dev/config.json"
    return cfg


# --------------------------------------------------------------------------
# config group itself
# --------------------------------------------------------------------------
def test_config_group_help_lists_subcommands(cli_runner):
    r = cli_runner.invoke(main, ["config", "--help"])
    assert r.exit_code == 0
    assert "show" in r.output
    assert "set" in r.output


# --------------------------------------------------------------------------
# config show
# --------------------------------------------------------------------------
def test_show_renders_identity_and_settings(cli_runner):
    cfg = _config_mock(github_user="bobthebuilder")
    with patch("gpu_dev_cli.cli.load_config", return_value=cfg) as lc:
        r = cli_runner.invoke(main, ["config", "show"])
    assert r.exit_code == 0
    lc.assert_called_once_with()
    # values pulled from the config double appear in the rendered panel
    assert "bobthebuilder" in r.output
    assert "pytorch-gpu-dev-reservation-queue" in r.output
    assert "pytorch-gpu-dev-cluster" in r.output
    assert "us-east-2" in r.output
    assert "123456789012" in r.output
    # the panel pulled identity + github username from the config
    cfg.get_user_identity.assert_called_once_with()
    cfg.get_github_username.assert_called_once_with()


def test_show_env_source_config_file_when_region_set(cli_runner):
    # config.get("region") truthy -> env_source = "Config file"
    cfg = _config_mock(user_cfg={"environment": "prod", "region": "us-east-2"})
    with patch("gpu_dev_cli.cli.load_config", return_value=cfg):
        r = cli_runner.invoke(main, ["config", "show"])
    assert r.exit_code == 0
    assert "Config file" in r.output
    assert "prod" in r.output


def test_show_env_source_default_when_no_region(cli_runner):
    # config.get("region") falsy -> env_source = "Default/ENV vars"
    cfg = _config_mock(user_cfg={"environment": None, "region": None})
    with patch("gpu_dev_cli.cli.load_config", return_value=cfg):
        r = cli_runner.invoke(main, ["config", "show"])
    assert r.exit_code == 0
    assert "Default" in r.output
    # environment falsy -> falls back to literal "Not set"
    assert "Not set" in r.output


def test_show_no_github_user_shows_not_set_hint(cli_runner):
    cfg = _config_mock(github_user=None)
    with patch("gpu_dev_cli.cli.load_config", return_value=cfg):
        r = cli_runner.invoke(main, ["config", "show"])
    assert r.exit_code == 0
    # github_user None -> "Not set - run: gpu-dev config set github_user ..."
    assert "Not set" in r.output


def test_show_swallows_exceptions_and_prints_error(cli_runner):
    # load_config raising is caught; command still exits 0 and prints the error.
    with patch("gpu_dev_cli.cli.load_config", side_effect=RuntimeError("boom")):
        r = cli_runner.invoke(main, ["config", "show"])
    assert r.exit_code == 0
    assert "Error" in r.output
    assert "boom" in r.output


# --------------------------------------------------------------------------
# config set
# --------------------------------------------------------------------------
def test_set_valid_github_user_saves(cli_runner):
    cfg = _config_mock()
    with patch("gpu_dev_cli.cli.load_config", return_value=cfg):
        r = cli_runner.invoke(main, ["config", "set", "github_user", "alice"])
    assert r.exit_code == 0
    cfg.save_config.assert_called_once_with("github_user", "alice")
    assert "github_user" in r.output
    assert "alice" in r.output
    assert "Saved" in r.output


def test_set_github_user_with_dots_is_accepted(cli_runner):
    # docstring promises dotted usernames work; value is passed through verbatim.
    cfg = _config_mock()
    with patch("gpu_dev_cli.cli.load_config", return_value=cfg):
        r = cli_runner.invoke(main, ["config", "set", "github_user", "jane.doe"])
    assert r.exit_code == 0
    cfg.save_config.assert_called_once_with("github_user", "jane.doe")
    assert "jane.doe" in r.output


def test_set_unknown_key_rejected_without_saving(cli_runner):
    cfg = _config_mock()
    with patch("gpu_dev_cli.cli.load_config", return_value=cfg):
        r = cli_runner.invoke(main, ["config", "set", "totally_bogus", "x"])
    assert r.exit_code == 0
    # early-return on invalid key: no save, and the valid-key list is surfaced.
    cfg.save_config.assert_not_called()
    assert "Unknown config key" in r.output
    assert "totally_bogus" in r.output
    assert "github_user" in r.output  # listed as the valid key


def test_set_unknown_key_is_case_sensitive(cli_runner):
    # whitelist check is exact-match: "GitHub_User" is not "github_user".
    cfg = _config_mock()
    with patch("gpu_dev_cli.cli.load_config", return_value=cfg):
        r = cli_runner.invoke(main, ["config", "set", "GitHub_User", "alice"])
    assert r.exit_code == 0
    cfg.save_config.assert_not_called()
    assert "Unknown config key" in r.output


def test_set_swallows_save_exception_and_prints_error(cli_runner):
    cfg = _config_mock()
    cfg.save_config.side_effect = OSError("disk full")
    with patch("gpu_dev_cli.cli.load_config", return_value=cfg):
        r = cli_runner.invoke(main, ["config", "set", "github_user", "alice"])
    # exception is caught inside the command -> exit 0, error surfaced.
    assert r.exit_code == 0
    assert "Error" in r.output
    assert "disk full" in r.output


def test_set_missing_value_arg_is_usage_error(cli_runner):
    # both KEY and VALUE are required click arguments.
    with patch("gpu_dev_cli.cli.load_config", return_value=_config_mock()) as lc:
        r = cli_runner.invoke(main, ["config", "set", "github_user"])
    assert r.exit_code == 2  # click usage error
    # click bails before the command body, so load_config is never called.
    lc.assert_not_called()


def test_set_missing_all_args_is_usage_error(cli_runner):
    with patch("gpu_dev_cli.cli.load_config", return_value=_config_mock()):
        r = cli_runner.invoke(main, ["config", "set"])
    assert r.exit_code == 2


@pytest.mark.parametrize("value", ["", "  ", "a-b_c.d"])
def test_set_passes_value_through_unmodified(cli_runner, value):
    # the command does not sanitize the value; whatever is given is saved.
    cfg = _config_mock()
    with patch("gpu_dev_cli.cli.load_config", return_value=cfg):
        r = cli_runner.invoke(main, ["config", "set", "github_user", value])
    assert r.exit_code == 0
    cfg.save_config.assert_called_once_with("github_user", value)
