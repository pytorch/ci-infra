"""Unit tests for the `gpu-dev debug` self-serve diagnostics command.

debug renders the status-history timeline, failure reason, OOM events, captured pod
logs, and recovery hints — all from DynamoDB fields (no cluster/lambda access).
"""
import re
from unittest.mock import MagicMock, patch

from gpu_dev_cli.cli import main


def _clean(s: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", s)


def _config():
    cfg = MagicMock()
    cfg.environment = "staging"  # avoids prod east1 cross-region fetch
    return cfg


def _ci(**overrides):
    base = {
        "ssh_command": "ssh dev@gpu-dev-9b1466cc",
        "reservation_id": "9b1466cc-f272-40a6-90da-2bf0f4c1e599",
        "status": "failed",
        "namespace": "gpu-dev",
        "gpu_count": 0,
        "gpu_type": "cpu-x86",
        "pod_name": "gpu-dev-9b1466cc",
        "instance_type": "c7i.8xlarge",
        "disk_name": "default",
        "ebs_volume_id": "vol-123",
        "launched_at": "2026-06-09T19:51:37",
        "expires_at": "2026-06-11T19:51:37",
        "created_at": "2026-06-09T19:51:00",
        "failure_reason": "Pod failed (Evicted): The node was low on resource: memory.",
        "current_detailed_status": "",
        "status_history": [
            {"timestamp": "2026-06-09T19:51:07", "message": "Fetching SSH keys"},
            {"timestamp": "2026-06-09T20:07:30", "message": "Container running"},
        ],
        "pod_logs": "",
        "oom_count": 0,
        "last_oom_at": "",
        "oom_container": "",
        "jupyter_url": "", "jupyter_port": "", "jupyter_token": "",
        "jupyter_enabled": False, "jupyter_error": "",
        "secondary_users": [],
        "warning": "",
        "is_multinode": False,
        "pod_ip": "", "node_ip": "", "node_name": "", "fqdn": "",
    }
    base.update(overrides)
    return base


def _invoke(cli_runner, mgr, args, fetch=None):
    from rich.console import Console
    with patch("gpu_dev_cli.cli.console", Console(width=240, force_terminal=False)), \
         patch("gpu_dev_cli.cli.load_config", return_value=_config()), \
         patch("gpu_dev_cli.cli.authenticate_user", return_value={"user_id": "alice@example.com"}), \
         patch("gpu_dev_cli.cli.ReservationManager", return_value=mgr), \
         patch("gpu_dev_cli.cli._fetch_reservations_cross_region", return_value=(fetch or [])):
        return cli_runner.invoke(main, ["debug"] + args)


def test_debug_explicit_id_shows_failure_timeline_and_recovery(cli_runner):
    mgr = MagicMock()
    mgr.get_connection_info.return_value = _ci()
    res = _invoke(cli_runner, mgr, ["9b1466cc"])
    out = _clean(res.output)
    assert res.exit_code == 0
    # failure reason surfaced (debug shows it even though `show` only does on 'failed')
    assert "Why it ended" in out and "low on resource: memory" in out
    # timeline rendered
    assert "Status timeline" in out and "Container running" in out
    # recovery hints for a terminal reservation with a disk
    assert "Recovery" in out
    assert "disk unlock default" in out
    mgr.get_connection_info.assert_called_once_with("9b1466cc", "alice@example.com")


def test_debug_oom_surfaced(cli_runner):
    mgr = MagicMock()
    mgr.get_connection_info.return_value = _ci(
        oom_count=3, last_oom_at="2026-06-09T20:00:00", oom_container="gpu-dev")
    res = _invoke(cli_runner, mgr, ["9b1466cc"])
    out = _clean(res.output)
    assert "OOM" in out and "3 event" in out


def test_debug_active_box_gives_cancel_recovery_hint(cli_runner):
    mgr = MagicMock()
    mgr.get_connection_info.return_value = _ci(status="active", failure_reason="")
    res = _invoke(cli_runner, mgr, ["9b1466cc"])
    out = _clean(res.output)
    # active-but-unreachable guidance: cancel to free the box + disk
    assert "Recovery" in out
    assert "gpu-dev cancel" in out


def test_debug_not_found(cli_runner):
    mgr = MagicMock()
    mgr.get_connection_info.return_value = None
    res = _invoke(cli_runner, mgr, ["nope"])
    out = _clean(res.output)
    assert "No reservation found" in out


def test_debug_no_id_no_active_points_to_list(cli_runner):
    mgr = MagicMock()
    res = _invoke(cli_runner, mgr, [], fetch=[])  # no active reservations
    out = _clean(res.output)
    assert "gpu-dev list" in out
    mgr.get_connection_info.assert_not_called()


def test_debug_no_id_auto_selects_single(cli_runner):
    mgr = MagicMock()
    mgr.get_connection_info.return_value = _ci(status="active", failure_reason="")
    res = _invoke(cli_runner, mgr, [], fetch=[{"reservation_id": "9b1466cc-aaa"}])
    out = _clean(res.output)
    assert res.exit_code == 0
    mgr.get_connection_info.assert_called_once_with("9b1466cc-aaa", "alice@example.com")


def test_debug_logs_flag_renders_lines(cli_runner):
    mgr = MagicMock()
    mgr.get_connection_info.return_value = _ci(status="failed")
    mgr.get_reservation_logs.return_value = {"lines": [
        {"timestamp": "2026-06-09T20:07:30", "message": "Creating pod gpu-dev-9b1466cc"},
        {"timestamp": "2026-06-09T20:55:00", "message": "Evicted: node low on memory"},
    ]}
    res = _invoke(cli_runner, mgr, ["9b1466cc", "--logs"])
    out = _clean(res.output)
    assert "Lambda logs" in out and "node low on memory" in out
    mgr.get_reservation_logs.assert_called_once_with("9b1466cc-f272-40a6-90da-2bf0f4c1e599",
                                                     "alice@example.com")


def test_debug_logs_flag_backend_unavailable(cli_runner):
    mgr = MagicMock()
    mgr.get_connection_info.return_value = _ci(status="failed")
    mgr.get_reservation_logs.return_value = None
    res = _invoke(cli_runner, mgr, ["9b1466cc", "--logs"])
    out = _clean(res.output)
    assert "Could not reach the log backend" in out


def test_debug_without_logs_flag_does_not_query(cli_runner):
    mgr = MagicMock()
    mgr.get_connection_info.return_value = _ci(status="failed")
    _invoke(cli_runner, mgr, ["9b1466cc"])
    mgr.get_reservation_logs.assert_not_called()
