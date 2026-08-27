"""Unit tests for `gpu-dev list` and `gpu-dev show` (cli.py).

Covers the pure formatting helpers (_format_gpu_display,
_format_expires_with_remaining) plus the `list` / `show` Click commands with a
fully-mocked ReservationManager / authenticate_user / load_config. No network,
AWS, or k8s.
"""
import re
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

from rich.console import Console

from gpu_dev_cli.cli import (
    main,
    _format_gpu_display,
    _format_expires_with_remaining,
)

# Sentinel distinguishing "caller left connection_info unset" from "explicitly None".
_UNSET = object()

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")


def _wide_console():
    """A wide Console so Rich never truncates table cells under test (the module
    creates its console at import time against whatever terminal pytest has)."""
    return Console(width=400, force_terminal=False)


def _clean(output: str) -> str:
    """Strip ANSI escape sequences so substring assertions are stable regardless
    of Rich's styling/highlighting (which can inject codes mid-word)."""
    return _ANSI_RE.sub("", output)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _iso_in(**delta):
    """ISO-8601 UTC timestamp offset from now by the given timedelta kwargs."""
    return (datetime.now(timezone.utc) + timedelta(**delta)).isoformat()


def _make_config(environment="staging"):
    """A fake Config whose user_config.environment is NOT 'prod' so the list
    command skips the cross-region east1 DynamoDB fetch entirely."""
    cfg = MagicMock(name="config")
    cfg.user_config = {"environment": environment}
    return cfg


def _patch_cli(reservations=None, connection_info=_UNSET, user_id="alice@example.com"):
    """Build a fully-mocked ReservationManager instance for `list`/`show`.

    connection_info=None is honored (sets get_connection_info -> None); leave it
    as the _UNSET sentinel to not configure that method at all."""
    mgr = MagicMock(name="ReservationManager-instance")
    if reservations is not None:
        mgr.list_reservations.return_value = list(reservations)
    if connection_info is not _UNSET:
        mgr.get_connection_info.return_value = connection_info
    return mgr


# --------------------------------------------------------------------------- #
# _format_gpu_display
# --------------------------------------------------------------------------- #
class TestFormatGpuDisplay:
    def test_plain_gpu_uppercases_type(self):
        assert _format_gpu_display(2, "h100") == "2x H100"

    def test_plain_gpu_single(self):
        assert _format_gpu_display(1, "b200") == "1x B200"

    def test_unknown_type_falls_back_to_count(self):
        assert _format_gpu_display(4, "unknown") == "4 GPU(s)"

    def test_empty_type_falls_back_to_count(self):
        assert _format_gpu_display(3, "") == "3 GPU(s)"

    def test_none_type_falls_back_to_count(self):
        assert _format_gpu_display(1, None) == "1 GPU(s)"

    def test_mig_1g_friendly(self):
        # MIG slices use the multiplication sign (×) and friendly mem label
        assert _format_gpu_display(2, "h100-mig-1g") == "2× 10GB H100 (MIG)"

    def test_mig_7g_friendly(self):
        assert _format_gpu_display(1, "h100-mig-7g") == "1× 80GB H100 (MIG)"

    def test_mig_b200_friendly(self):
        assert _format_gpu_display(1, "b200-mig-3g") == "1× 90GB B200 (MIG)"

    def test_mig_case_insensitive(self):
        # gt_lower lowercases before the mig_friendly lookup
        assert _format_gpu_display(1, "H100-MIG-1G") == "1× 10GB H100 (MIG)"


# --------------------------------------------------------------------------- #
# _format_expires_with_remaining
# --------------------------------------------------------------------------- #
class TestFormatExpiresWithRemaining:
    def test_na_passthrough(self):
        assert _format_expires_with_remaining("N/A") == "N/A"

    def test_empty_passthrough(self):
        assert _format_expires_with_remaining("") == "N/A"
        assert _format_expires_with_remaining(None) == "N/A"

    def test_already_expired(self):
        out = _format_expires_with_remaining(_iso_in(hours=-1))
        assert out.endswith("(expired)")

    def test_hours_and_minutes_left(self):
        out = _format_expires_with_remaining(_iso_in(hours=2, minutes=30))
        # ~2h29m or 2h30m depending on rounding of seconds; assert the shape
        assert "left)" in out
        assert "h" in out and "m left" in out

    def test_minutes_only_left(self):
        out = _format_expires_with_remaining(_iso_in(minutes=45))
        assert "m left)" in out
        assert "h" not in out.split("(")[1]  # no hours component in remaining

    def test_hours_only_no_minutes(self):
        # Exactly 3 hours -> "3h left" (no minute component)
        out = _format_expires_with_remaining(_iso_in(hours=3, seconds=2))
        assert "3h left)" in out

    def test_z_suffix_parsed(self):
        ts = (datetime.now(timezone.utc) + timedelta(minutes=30)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        out = _format_expires_with_remaining(ts)
        assert "left)" in out

    def test_invalid_string_returns_invalid(self):
        assert _format_expires_with_remaining("not-a-timestamp") == "Invalid"

    def test_legacy_unix_timestamp(self):
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()
        out = _format_expires_with_remaining(future)
        assert "left)" in out


# --------------------------------------------------------------------------- #
# `gpu-dev list`
# --------------------------------------------------------------------------- #
class TestListCommand:
    def _invoke(self, cli_runner, mgr, args=None, environment="staging"):
        with patch("gpu_dev_cli.cli.console", _wide_console()), \
             patch("gpu_dev_cli.cli.load_config", return_value=_make_config(environment)), \
             patch("gpu_dev_cli.cli.authenticate_user",
                   return_value={"user_id": "alice@example.com"}), \
             patch("gpu_dev_cli.cli.ReservationManager", return_value=mgr):
            return cli_runner.invoke(main, ["list"] + (args or []))

    def test_empty_state(self, cli_runner):
        mgr = _patch_cli(reservations=[])
        result = self._invoke(cli_runner, mgr)
        assert result.exit_code == 0
        assert "No reservations found" in _clean(result.output)

    def test_default_filters_to_current_user(self, cli_runner):
        mgr = _patch_cli(reservations=[])
        self._invoke(cli_runner, mgr)
        # default view: list_reservations called with the authenticated user_id
        called_user_filters = [
            c.kwargs.get("user_filter") for c in mgr.list_reservations.call_args_list
        ]
        assert "alice@example.com" in called_user_filters

    def test_user_all_passes_none_filter(self, cli_runner):
        mgr = _patch_cli(reservations=[])
        self._invoke(cli_runner, mgr, ["--user", "all"])
        called_user_filters = [
            c.kwargs.get("user_filter") for c in mgr.list_reservations.call_args_list
        ]
        # --user all => user_filter None for all underlying calls
        assert all(uf is None for uf in called_user_filters)

    def test_specific_user_filter(self, cli_runner):
        mgr = _patch_cli(reservations=[])
        self._invoke(cli_runner, mgr, ["--user", "bob@x.com"])
        called_user_filters = [
            c.kwargs.get("user_filter") for c in mgr.list_reservations.call_args_list
        ]
        assert "bob@x.com" in called_user_filters

    def test_invalid_status_rejected(self, cli_runner):
        mgr = _patch_cli(reservations=[])
        result = self._invoke(cli_runner, mgr, ["--status", "bogus"])
        # invalid status -> fetch_and_display returns False, no list_reservations call
        assert "Invalid status" in _clean(result.output)
        assert "bogus" in _clean(result.output)
        mgr.list_reservations.assert_not_called()

    def test_status_all_uses_none(self, cli_runner):
        mgr = _patch_cli(reservations=[])
        self._invoke(cli_runner, mgr, ["--status", "all"])
        # status=all => statuses_to_include None
        statuses = [
            c.kwargs.get("statuses_to_include")
            for c in mgr.list_reservations.call_args_list
        ]
        assert None in statuses

    def test_explicit_status_parsed(self, cli_runner):
        mgr = _patch_cli(reservations=[])
        self._invoke(cli_runner, mgr, ["--status", "active,expired"])
        statuses = [
            c.kwargs.get("statuses_to_include")
            for c in mgr.list_reservations.call_args_list
        ]
        assert ["active", "expired"] in statuses

    def test_renders_active_reservation_row(self, cli_runner):
        res = {
            "reservation_id": "abcd1234ef567890",
            "user_id": "alice@example.com",
            "gpu_count": 2,
            "gpu_type": "h100",
            "status": "active",
            "created_at": _iso_in(hours=-1),
            "expires_at": _iso_in(hours=5),
            "ssh_command": "ssh dev@1.2.3.4 -p 22",
        }
        mgr = _patch_cli(reservations=[res])
        result = self._invoke(cli_runner, mgr)
        assert result.exit_code == 0
        # ID truncated to 8 chars
        assert "abcd1234" in _clean(result.output)
        # username before '@'
        assert "alice" in _clean(result.output)
        # gpu display
        assert "H100" in _clean(result.output)
        assert "active" in _clean(result.output)
        # active row shows Ready hint with the node host
        assert "Ready" in _clean(result.output)
        assert "GPU Reservations" in _clean(result.output)

    def test_renders_queued_reservation_eta(self, cli_runner):
        res = {
            "reservation_id": "queued00aaaa",
            "user_id": "alice@example.com",
            "gpu_count": 1,
            "gpu_type": "b200",
            "status": "queued",
            "created_at": _iso_in(minutes=-5),
            "estimated_wait_minutes": 12,
        }
        mgr = _patch_cli(reservations=[res])
        result = self._invoke(cli_runner, mgr)
        assert result.exit_code == 0
        assert "queued" in _clean(result.output)
        # ETA rendered as ~Nmin
        assert "~12min" in _clean(result.output)

    def test_queued_without_eta_shows_waiting(self, cli_runner):
        res = {
            "reservation_id": "queuednoeta1",
            "user_id": "alice@example.com",
            "gpu_count": 1,
            "gpu_type": "b200",
            "status": "queued",
            "created_at": _iso_in(minutes=-5),
        }
        mgr = _patch_cli(reservations=[res])
        result = self._invoke(cli_runner, mgr)
        assert result.exit_code == 0
        assert "Waiting" in _clean(result.output)

    def test_storage_disk_name_shown(self, cli_runner):
        res = {
            "reservation_id": "diskres00",
            "user_id": "alice@example.com",
            "gpu_count": 1,
            "gpu_type": "h100",
            "status": "active",
            "created_at": _iso_in(hours=-1),
            "expires_at": _iso_in(hours=5),
            "disk_name": "mydisk",
            "ssh_command": "ssh dev@h1 -p 22",
        }
        mgr = _patch_cli(reservations=[res])
        result = self._invoke(cli_runner, mgr)
        assert "disk: mydisk" in _clean(result.output)

    def test_storage_persistent_via_ebs(self, cli_runner):
        res = {
            "reservation_id": "ebsres000",
            "user_id": "alice@example.com",
            "gpu_count": 1,
            "gpu_type": "h100",
            "status": "active",
            "created_at": _iso_in(hours=-1),
            "expires_at": _iso_in(hours=5),
            "ebs_volume_id": "vol-123",
            "ssh_command": "ssh dev@h1 -p 22",
        }
        mgr = _patch_cli(reservations=[res])
        result = self._invoke(cli_runner, mgr)
        assert "persistent" in _clean(result.output)

    def test_storage_temporary_default(self, cli_runner):
        res = {
            "reservation_id": "tmpres000",
            "user_id": "alice@example.com",
            "gpu_count": 1,
            "gpu_type": "h100",
            "status": "active",
            "created_at": _iso_in(hours=-1),
            "expires_at": _iso_in(hours=5),
            "ssh_command": "ssh dev@h1 -p 22",
        }
        mgr = _patch_cli(reservations=[res])
        result = self._invoke(cli_runner, mgr)
        assert "temporary" in _clean(result.output)

    def test_active_oom_indicator(self, cli_runner):
        res = {
            "reservation_id": "oomres000",
            "user_id": "alice@example.com",
            "gpu_count": 1,
            "gpu_type": "h100",
            "status": "active",
            "created_at": _iso_in(hours=-1),
            "expires_at": _iso_in(hours=5),
            "oom_count": 3,
            "ssh_command": "ssh dev@h1 -p 22",
        }
        mgr = _patch_cli(reservations=[res])
        result = self._invoke(cli_runner, mgr)
        assert "OOM x3" in _clean(result.output)

    def test_failed_reason_truncated(self, cli_runner):
        res = {
            "reservation_id": "failres00",
            "user_id": "alice@example.com",
            "gpu_count": 1,
            "gpu_type": "h100",
            "status": "failed",
            "created_at": _iso_in(minutes=-10),
            "failure_reason": "something went very very wrong here in detail",
        }
        mgr = _patch_cli(reservations=[res])
        result = self._invoke(cli_runner, mgr, ["--status", "failed"])
        assert "failed" in _clean(result.output)
        # reason first line truncated to 20 chars: "something went very "
        assert "something went very" in _clean(result.output)

    def test_cancelled_status_rendered(self, cli_runner):
        res = {
            "reservation_id": "cancres00",
            "user_id": "alice@example.com",
            "gpu_count": 1,
            "gpu_type": "h100",
            "status": "cancelled",
            "created_at": _iso_in(minutes=-10),
            "reservation_ended": _iso_in(minutes=-5),
        }
        mgr = _patch_cli(reservations=[res])
        result = self._invoke(cli_runner, mgr, ["--status", "cancelled"])
        assert "cancelled" in _clean(result.output)
        assert "Cancelled" in _clean(result.output)  # expires column

    def test_details_flag_adds_versions(self, cli_runner):
        res = {
            "reservation_id": "verres000",
            "user_id": "alice@example.com",
            "gpu_count": 1,
            "gpu_type": "h100",
            "status": "active",
            "created_at": _iso_in(hours=-1),
            "expires_at": _iso_in(hours=5),
            "cli_version": "0.6.6",
            "lambda_version": "0.7.0",
            "ssh_command": "ssh dev@h1 -p 22",
        }
        mgr = _patch_cli(reservations=[res])
        result = self._invoke(cli_runner, mgr, ["--details"])
        assert "0.6.6" in _clean(result.output)
        assert "0.7.0" in _clean(result.output)

    def test_details_flag_missing_versions_defaults(self, cli_runner):
        res = {
            "reservation_id": "verres001",
            "user_id": "alice@example.com",
            "gpu_count": 1,
            "gpu_type": "h100",
            "status": "active",
            "created_at": _iso_in(hours=-1),
            "expires_at": _iso_in(hours=5),
            "ssh_command": "ssh dev@h1 -p 22",
        }
        mgr = _patch_cli(reservations=[res])
        result = self._invoke(cli_runner, mgr, ["--details"])
        # missing versions fall back to the sentinel strings
        assert "<0.2.5" in _clean(result.output)
        assert "<0.2.6" in _clean(result.output)

    def test_auth_runtime_error_reported(self, cli_runner):
        mgr = _patch_cli(reservations=[])
        with patch("gpu_dev_cli.cli.load_config", return_value=_make_config()), \
             patch("gpu_dev_cli.cli.authenticate_user",
                   side_effect=RuntimeError("no creds")), \
             patch("gpu_dev_cli.cli.ReservationManager", return_value=mgr):
            result = cli_runner.invoke(main, ["list"])
        assert "no creds" in _clean(result.output)
        mgr.list_reservations.assert_not_called()

    def test_malformed_reservation_skipped(self, cli_runner):
        # gpu_count is a non-int-coercible value to trip the per-row try/except
        bad = {
            "reservation_id": "badrow000",
            "user_id": "alice@example.com",
            "gpu_count": 1,
            "gpu_type": "h100",
            "status": "active",
            "created_at": _iso_in(hours=-1),
            "expires_at": _iso_in(hours=5),
            "oom_count": "not-an-int",  # int(oom_count) raises in the row loop
            "ssh_command": "ssh dev@h1 -p 22",
        }
        good = {
            "reservation_id": "goodrow00",
            "user_id": "alice@example.com",
            "gpu_count": 1,
            "gpu_type": "b200",
            "status": "active",
            "created_at": _iso_in(hours=-1),
            "expires_at": _iso_in(hours=5),
            "ssh_command": "ssh dev@b1 -p 22",
        }
        mgr = _patch_cli(reservations=[bad, good])
        result = self._invoke(cli_runner, mgr)
        assert result.exit_code == 0
        # malformed row is skipped with a warning, good row still renders
        assert "Skipping malformed reservation" in _clean(result.output)
        assert "goodrow0" in _clean(result.output)


# --------------------------------------------------------------------------- #
# `gpu-dev show`
# --------------------------------------------------------------------------- #
class TestShowCommand:
    def _invoke(self, cli_runner, mgr, args=None):
        with patch("gpu_dev_cli.cli.console", _wide_console()), \
             patch("gpu_dev_cli.cli.load_config", return_value=_make_config()), \
             patch("gpu_dev_cli.cli.authenticate_user",
                   return_value={"user_id": "alice@example.com"}), \
             patch("gpu_dev_cli.cli.ReservationManager", return_value=mgr):
            return cli_runner.invoke(main, ["show"] + (args or []))

    def test_show_no_id_empty(self, cli_runner):
        mgr = _patch_cli(reservations=[])
        result = self._invoke(cli_runner, mgr)
        assert result.exit_code == 0
        assert "No reservations found" in _clean(result.output)
        # never fetched connection info because nothing to show
        mgr.get_connection_info.assert_not_called()

    def test_show_no_id_lists_active(self, cli_runner):
        res = {"reservation_id": "showme00aaaa", "status": "active"}
        conn = {
            "reservation_id": "showme00aaaa",
            "status": "active",
            "gpu_count": 1,
            "gpu_type": "h100",
            "instance_type": "p5.48xlarge",
            "pod_name": "gpu-dev-pod-1",
            "ssh_command": "ssh dev@1.2.3.4 -p 22",
            "launched_at": _iso_in(hours=-1),
            "expires_at": _iso_in(hours=5),
        }
        mgr = _patch_cli(reservations=[res], connection_info=conn)
        result = self._invoke(cli_runner, mgr)
        assert result.exit_code == 0
        # list_reservations filtered to current user + active statuses
        mgr.list_reservations.assert_called_once_with(
            user_filter="alice@example.com",
            statuses_to_include=["active", "preparing", "queued", "pending"],
        )
        mgr.get_connection_info.assert_called_once_with(
            "showme00aaaa", "alice@example.com"
        )
        assert "Active Reservation" in _clean(result.output)
        assert "gpu-dev-pod-1" in _clean(result.output)
        assert "p5.48xlarge" in _clean(result.output)

    def test_show_specific_id(self, cli_runner):
        conn = {
            "reservation_id": "specific0000",
            "status": "active",
            "gpu_count": 4,
            "gpu_type": "b200",
            "instance_type": "p6-b200.48xlarge",
            "pod_name": "gpu-dev-pod-x",
            "ssh_command": "ssh dev@9.9.9.9 -p 22",
            "launched_at": _iso_in(hours=-2),
            "expires_at": _iso_in(hours=3),
        }
        mgr = _patch_cli(connection_info=conn)
        result = self._invoke(cli_runner, mgr, ["specific0"])
        assert result.exit_code == 0
        mgr.get_connection_info.assert_called_once_with(
            "specific0", "alice@example.com"
        )
        # list_reservations not called when an explicit id is given
        mgr.list_reservations.assert_not_called()
        assert "Active Reservation" in _clean(result.output)
        assert "B200" in _clean(result.output)

    def test_show_specific_id_not_found(self, cli_runner):
        mgr = _patch_cli(connection_info=None)
        result = self._invoke(cli_runner, mgr, ["missing0"])
        assert result.exit_code == 0
        assert "Could not get connection info" in _clean(result.output)
        assert "missing0" in _clean(result.output)

    def test_show_queued_panel(self, cli_runner):
        conn = {
            "reservation_id": "queuedshow00",
            "status": "queued",
            "gpu_count": 2,
            "gpu_type": "h100",
            "instance_type": "unknown",
        }
        mgr = _patch_cli(connection_info=conn)
        result = self._invoke(cli_runner, mgr, ["queuedsh"])
        assert result.exit_code == 0
        # queued panel shows TBD instance + GPU request line
        assert "Queued Reservation" in _clean(result.output)
        assert "TBD" in _clean(result.output)
        assert "SSH access will be available" in _clean(result.output)

    def test_show_auth_error(self, cli_runner):
        mgr = _patch_cli(connection_info=None)
        with patch("gpu_dev_cli.cli.load_config", return_value=_make_config()), \
             patch("gpu_dev_cli.cli.authenticate_user",
                   side_effect=RuntimeError("auth boom")), \
             patch("gpu_dev_cli.cli.ReservationManager", return_value=mgr):
            result = cli_runner.invoke(main, ["show", "anyid000"])
        assert "auth boom" in _clean(result.output)
        mgr.get_connection_info.assert_not_called()
