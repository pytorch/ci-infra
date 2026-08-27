"""Unit tests for `gpu-dev avail` / `_show_availability` (cli.py).

Focus: spot SKUs (cpu-spot + the us-east-1 spot section) are HIDDEN by default,
`--spot` REVEALS them, and the command never hides everything when the
environment is spot-only.

All AWS / auth / k8s access is mocked. `load_config`, `authenticate_user` and
`ReservationManager` are patched where the cli module looks them up
(`gpu_dev_cli.cli.*`). Output is captured via CliRunner (the Rich `console`
resolves `sys.stdout` lazily, so it lands in the captured buffer).
"""
import re
from unittest.mock import MagicMock, patch

import pytest

from gpu_dev_cli.cli import main

_ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")


def _clean(output: str) -> str:
    """Strip ANSI escape codes so substring asserts survive Rich styling that
    spans color/style boundaries (e.g. the dim '~70% cheaper' footnote)."""
    return _ANSI.sub("", output)


# --- helpers ------------------------------------------------------------------

def _make_config(environment="prod", east1_items=None):
    """A fake Config whose .session.resource(...).Table(...).scan() returns
    the given east1 availability items (used only on the --spot prod path)."""
    cfg = MagicMock(name="config")
    cfg.user_config = {"environment": environment}
    east1_table = MagicMock(name="east1_table")
    east1_table.scan.return_value = {"Items": east1_items or []}
    resource = MagicMock(name="resource")
    resource.Table.return_value = east1_table
    cfg.session.resource.return_value = resource
    cfg._east1_table = east1_table  # convenience handle for assertions
    return cfg


def _make_mgr(availability):
    mgr = MagicMock(name="reservation_mgr")
    mgr.get_gpu_availability_by_type.return_value = availability
    return mgr


def _run(runner, cfg, mgr, args):
    """Invoke `gpu-dev <args>` with load_config/authenticate_user/ReservationManager
    patched. Returns the click Result."""
    with patch("gpu_dev_cli.cli.load_config", return_value=cfg), \
         patch("gpu_dev_cli.cli.authenticate_user", return_value={"github_user": "tester"}) as auth, \
         patch("gpu_dev_cli.cli.ReservationManager", return_value=mgr):
        result = runner.invoke(main, args, catch_exceptions=False)
    result._auth = auth
    result._clean = _clean(result.output)
    return result


# --- fixtures -----------------------------------------------------------------

@pytest.fixture(autouse=True)
def _wide_terminal(monkeypatch):
    """Force a wide Rich render so table cells aren't truncated under an 80-col
    default (no tty in tests)."""
    monkeypatch.setenv("COLUMNS", "240")


@pytest.fixture
def mixed_availability():
    """A normal prod fleet: one real GPU type plus a cpu-spot SKU."""
    return {
        "b200": {
            "available": 8, "total": 8, "max_reservable": 8,
            "queue_length": 0, "full_nodes_available": 1,
        },
        "cpu-spot": {
            "available": 0, "total": 100, "max_reservable": 4,
            "queue_length": 2, "estimated_wait_minutes": 0,
        },
    }


# --- default behavior: spot hidden --------------------------------------------

def test_default_hides_cpu_spot_row(cli_runner, mixed_availability):
    cfg = _make_config()
    mgr = _make_mgr(mixed_availability)
    r = _run(cli_runner, cfg, mgr, ["avail"])

    assert r.exit_code == 0
    # Real GPU type is shown, spot SKU is hidden from the full table.
    assert "B200" in r._clean
    assert "CPU-SPOT" not in r._clean
    # The "hidden" hint is printed because non-spot types exist.
    assert "Spot instances hidden" in r._clean


def test_default_does_not_fetch_east1_spot_section(cli_runner, mixed_availability):
    """Without --spot the prod east1 spot table is never fetched nor shown."""
    cfg = _make_config(east1_items=[
        {"gpu_type": "h100", "available_gpus": 8, "total_gpus": 8,
         "max_reservable": 8, "spot_info": {"spot_price": "10"}},
    ])
    mgr = _make_mgr(mixed_availability)
    r = _run(cli_runner, cfg, mgr, ["avail"])

    assert r.exit_code == 0
    assert "Spot Instances" not in r._clean
    # _fetch_east1_spot short-circuits before touching DynamoDB.
    assert cfg.session.resource.called is False
    assert cfg._east1_table.scan.called is False


def test_default_still_shows_non_spot_table(cli_runner):
    """Default view renders the full GPU section (non-spot) and the legend."""
    avail = {
        "h100": {"available": 4, "total": 8, "max_reservable": 8,
                 "queue_length": 0, "full_nodes_available": 0},
    }
    cfg = _make_config()
    mgr = _make_mgr(avail)
    r = _run(cli_runner, cfg, mgr, ["avail"])

    assert r.exit_code == 0
    assert "H100" in r._clean
    assert "Full GPUs" in r._clean
    assert "Availability legend" in r._clean


# --- --spot reveals everything ------------------------------------------------

def test_spot_flag_reveals_cpu_spot_row(cli_runner, mixed_availability):
    cfg = _make_config()
    mgr = _make_mgr(mixed_availability)
    r = _run(cli_runner, cfg, mgr, ["avail", "--spot"])

    assert r.exit_code == 0
    assert "B200" in r._clean
    assert "CPU-SPOT" in r._clean
    # No "hidden" hint when spot is shown.
    assert "Spot instances hidden" not in r._clean


def test_spot_flag_fetches_and_shows_east1_section(cli_runner, mixed_availability):
    cfg = _make_config(east1_items=[
        {"gpu_type": "h100", "available_gpus": 8, "total_gpus": 8,
         "max_reservable": 8, "spot_info": {"spot_price": "10", "spot_signal": "ok"}},
        {"gpu_type": "t4", "available_gpus": 0, "total_gpus": 4,
         "max_reservable": 4, "spot_info": {}},
    ])
    mgr = _make_mgr(mixed_availability)
    r = _run(cli_runner, cfg, mgr, ["avail", "--spot"])

    assert r.exit_code == 0
    # East1 spot table was fetched and rendered.
    assert cfg._east1_table.scan.called is True
    assert "Spot Instances" in r._clean
    # Spot rows carry the " *" marker and the footnote.
    assert "H100 *" in r._clean
    assert "T4 *" in r._clean
    assert "spot: ~70% cheaper" in r._clean


def test_spot_flag_only_includes_known_east1_spot_types(cli_runner, mixed_availability):
    """Items whose gpu_type is not in prod-east1 spot_types are dropped."""
    cfg = _make_config(east1_items=[
        {"gpu_type": "h100", "available_gpus": 1, "total_gpus": 8,
         "max_reservable": 8, "spot_info": {}},
        {"gpu_type": "not-a-spot-type", "available_gpus": 9, "total_gpus": 9,
         "max_reservable": 9, "spot_info": {}},
    ])
    mgr = _make_mgr(mixed_availability)
    r = _run(cli_runner, cfg, mgr, ["avail", "--spot"])

    assert r.exit_code == 0
    assert "H100 *" in r._clean
    assert "NOT-A-SPOT-TYPE" not in r._clean


def test_spot_flag_computes_discount_percentage(cli_runner, mixed_availability):
    """spot_price 49 vs h100 on-demand 98 => 50% off displayed."""
    cfg = _make_config(east1_items=[
        {"gpu_type": "h100", "available_gpus": 8, "total_gpus": 8,
         "max_reservable": 8, "spot_info": {"spot_price": "49", "spot_signal": "ok"}},
    ])
    mgr = _make_mgr(mixed_availability)
    r = _run(cli_runner, cfg, mgr, ["avail", "--spot"])

    assert r.exit_code == 0
    assert "50% off on-demand" in r._clean


# --- never hide everything when env is spot-only ------------------------------

def test_spot_only_availability_not_hidden_without_flag(cli_runner):
    """If every (non-mig) SKU is a spot SKU, nothing is hidden even by default —
    `_non_spot_exists` is False so `_hide_spot` stays False."""
    avail = {
        "cpu-spot": {"available": 5, "total": 100, "max_reservable": 4,
                     "queue_length": 0},
        "h100-spot": {"available": 0, "total": 8, "max_reservable": 8,
                      "queue_length": 1, "estimated_wait_minutes": 0},
    }
    cfg = _make_config()
    mgr = _make_mgr(avail)
    r = _run(cli_runner, cfg, mgr, ["avail"])

    assert r.exit_code == 0
    assert "CPU-SPOT" in r._clean
    assert "H100-SPOT" in r._clean
    # No "hidden" hint, because nothing was actually hidden.
    assert "Spot instances hidden" not in r._clean


def test_mig_only_does_not_count_as_non_spot(cli_runner):
    """`_non_spot_exists` ignores mig keys, so a fleet of (mig + cpu-spot) still
    treats cpu-spot as the only full-type SKU and does NOT hide it."""
    avail = {
        "h100-mig-1g": {"available": 7, "total": 7, "max_reservable": 7,
                        "queue_length": 0},
        "cpu-spot": {"available": 3, "total": 100, "max_reservable": 4,
                     "queue_length": 0},
    }
    cfg = _make_config()
    mgr = _make_mgr(avail)
    r = _run(cli_runner, cfg, mgr, ["avail"])

    assert r.exit_code == 0
    # cpu-spot kept (it's the only non-mig type, so _non_spot_exists is False).
    assert "CPU-SPOT" in r._clean
    assert "Spot instances hidden" not in r._clean
    # MIG section is rendered separately.
    assert "MIG Slices" in r._clean


# --- error / edge branches ----------------------------------------------------

def test_runtime_error_prints_message_and_returns(cli_runner):
    """A RuntimeError from authenticate_user is caught, printed, and the command
    exits 0 without raising."""
    cfg = _make_config()
    mgr = _make_mgr({"h100": {"available": 1, "total": 8}})
    with patch("gpu_dev_cli.cli.load_config", return_value=cfg), \
         patch("gpu_dev_cli.cli.authenticate_user", side_effect=RuntimeError("no creds")), \
         patch("gpu_dev_cli.cli.ReservationManager", return_value=mgr):
        r = cli_runner.invoke(main, ["avail"], catch_exceptions=False)

    assert r.exit_code == 0
    assert "no creds" in _clean(r.output)
    # We never got as far as fetching availability.
    mgr.get_gpu_availability_by_type.assert_not_called()


def test_empty_availability_prints_could_not_get(cli_runner):
    cfg = _make_config()
    mgr = _make_mgr({})
    r = _run(cli_runner, cfg, mgr, ["avail"])

    assert r.exit_code == 0
    assert "Could not get GPU availability" in r._clean


def test_default_calls_availability_fetch_once(cli_runner, mixed_availability):
    cfg = _make_config()
    mgr = _make_mgr(mixed_availability)
    r = _run(cli_runner, cfg, mgr, ["avail"])

    assert r.exit_code == 0
    mgr.get_gpu_availability_by_type.assert_called_once_with()
    r._auth.assert_called_once()


def test_watch_flag_routes_to_watch_helper(cli_runner):
    """`--watch` dispatches to `_show_availability_watch`, not the one-shot path."""
    with patch("gpu_dev_cli.cli._show_availability_watch") as watch, \
         patch("gpu_dev_cli.cli._show_availability") as once:
        r = cli_runner.invoke(main, ["avail", "--watch", "--interval", "9"],
                              catch_exceptions=False)

    assert r.exit_code == 0
    watch.assert_called_once_with(9, show_spot=False)
    once.assert_not_called()


def test_no_watch_routes_to_one_shot_with_spot_flag(cli_runner):
    with patch("gpu_dev_cli.cli._show_availability_watch") as watch, \
         patch("gpu_dev_cli.cli._show_availability") as once:
        r = cli_runner.invoke(main, ["avail", "--spot"], catch_exceptions=False)

    assert r.exit_code == 0
    once.assert_called_once_with(show_spot=True)
    watch.assert_not_called()
