"""Smoke test: the CLI is invokable in-process."""
from gpu_dev_cli.cli import main

def test_help(cli_runner):
    r = cli_runner.invoke(main, ["--help"])
    assert r.exit_code == 0
    assert "reserve" in r.output

def test_repro_help_has_no_connect(cli_runner):
    r = cli_runner.invoke(main, ["repro", "--help"])
    assert r.exit_code == 0
    assert "--no-connect" in r.output
