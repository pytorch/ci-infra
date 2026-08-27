"""Unit tests for `gpu-dev edit` (cli.py:edit).

Covers the command-line (non-interactive) paths of the edit command:
  - validation guards (enable+disable conflict, no-action error, missing id)
  - reservation lookup (not found / not active / id-prefix normalization)
  - --extend bounds (<=0, >24) + success/failure
  - --add-user success/failure (+ the post-success hints)
  - --enable-jupyter / --disable-jupyter success/failure
  - the top-level exception handler

We force `--no-interactive` so the interactive selection branch is skipped, and
patch the symbols where edit looks them up: in ``gpu_dev_cli.cli``
(ReservationManager, load_config, authenticate_user). The manager is a MagicMock
whose get_connection_info returns a fake reservation dict; edit reads
``reservation_id`` + ``status`` off it.
"""
from unittest.mock import MagicMock, patch

import pytest

from gpu_dev_cli.cli import main

RID = "abc12345-dead-beef-cafe-001122334455"
UID = "octocat"


def _conn_info(reservation_id=RID, status="active"):
    """A minimal connection_info dict as get_connection_info returns it.

    edit() only consumes ``reservation_id`` (to normalize the prefix) and
    ``status`` (to gate on active).
    """
    return {"reservation_id": reservation_id, "status": status}


def _patch_mgr(connection_info=None, **method_returns):
    """Build (rm_cls, mgr). mgr.get_connection_info returns connection_info;
    other manager methods take their value from **method_returns (default True).
    """
    mgr = MagicMock(name="ReservationManager_instance")
    mgr.get_connection_info.return_value = (
        connection_info if connection_info is not None else _conn_info()
    )
    for meth in ("extend_reservation", "add_user", "enable_jupyter", "disable_jupyter"):
        getattr(mgr, meth).return_value = method_returns.get(meth, True)
    rm_cls = MagicMock(name="ReservationManager", return_value=mgr)
    return rm_cls, mgr


def _invoke(cli_runner, args, rm_cls, *, auth_user=UID, auth_side_effect=None):
    """Invoke `gpu-dev edit` with the common patches applied."""
    auth_kwargs = {}
    if auth_side_effect is not None:
        auth_kwargs["side_effect"] = auth_side_effect
    else:
        auth_kwargs["return_value"] = {"user_id": auth_user}

    with patch("gpu_dev_cli.cli.ReservationManager", rm_cls), \
         patch("gpu_dev_cli.cli.load_config", return_value=MagicMock(name="config")), \
         patch("gpu_dev_cli.cli.authenticate_user", **auth_kwargs):
        return cli_runner.invoke(main, args)


# --------------------------------------------------------------------------- #
# Validation guards                                                            #
# --------------------------------------------------------------------------- #
def test_enable_and_disable_jupyter_conflict(cli_runner):
    rm_cls, mgr = _patch_mgr()
    result = _invoke(
        cli_runner,
        ["edit", RID, "--no-interactive", "--enable-jupyter", "--disable-jupyter"],
        rm_cls,
    )
    assert result.exit_code == 0, result.output
    assert "Cannot enable and disable Jupyter at the same time" in result.output
    # Bailed before contacting the service.
    mgr.get_connection_info.assert_not_called()
    mgr.enable_jupyter.assert_not_called()
    mgr.disable_jupyter.assert_not_called()


def test_no_action_specified_errors(cli_runner):
    """An id but no action flag, non-interactive -> the 'please specify' guard."""
    rm_cls, mgr = _patch_mgr()
    result = _invoke(cli_runner, ["edit", RID, "--no-interactive"], rm_cls)
    assert result.exit_code == 0, result.output
    assert "Please specify --enable-jupyter, --disable-jupyter, --add-user, or --extend" in result.output
    mgr.get_connection_info.assert_not_called()


def test_no_reservation_id_with_action_errors(cli_runner):
    """An action but no id, non-interactive -> 'No reservation ID provided'."""
    rm_cls, mgr = _patch_mgr()
    result = _invoke(cli_runner, ["edit", "--no-interactive", "--extend", "2"], rm_cls)
    assert result.exit_code == 0, result.output
    assert "No reservation ID provided" in result.output
    mgr.get_connection_info.assert_not_called()
    mgr.extend_reservation.assert_not_called()


# --------------------------------------------------------------------------- #
# Reservation lookup                                                           #
# --------------------------------------------------------------------------- #
def test_reservation_not_found(cli_runner):
    rm_cls, mgr = _patch_mgr(connection_info=None)
    # connection_info None -> not found branch; reset get_connection_info return.
    mgr.get_connection_info.return_value = None
    result = _invoke(cli_runner, ["edit", "deadbeef", "--no-interactive", "--extend", "2"], rm_cls)
    assert result.exit_code == 0, result.output
    assert "not found or doesn't belong to you" in result.output
    # Lookup attempted with the user-provided prefix + authed user id.
    mgr.get_connection_info.assert_called_once_with("deadbeef", UID)
    mgr.extend_reservation.assert_not_called()


def test_not_found_message_truncates_id_to_8(cli_runner):
    rm_cls, mgr = _patch_mgr()
    mgr.get_connection_info.return_value = None
    result = _invoke(
        cli_runner,
        ["edit", "abcd1234extra", "--no-interactive", "--add-user", "friend"],
        rm_cls,
    )
    assert "Reservation abcd1234 not found" in result.output


def test_non_active_reservation_rejected(cli_runner):
    rm_cls, mgr = _patch_mgr(connection_info=_conn_info(status="queued"))
    result = _invoke(cli_runner, ["edit", RID, "--no-interactive", "--extend", "2"], rm_cls)
    assert result.exit_code == 0, result.output
    assert "Can only edit active reservations" in result.output
    assert "current status: queued" in result.output
    mgr.extend_reservation.assert_not_called()


def test_uses_full_id_from_connection_info_not_prefix(cli_runner):
    """edit normalizes reservation_id to the full id returned by get_connection_info."""
    full = "abc12345-1111-2222-3333-444455556666"
    rm_cls, mgr = _patch_mgr(connection_info=_conn_info(reservation_id=full))
    result = _invoke(cli_runner, ["edit", "abc12345", "--no-interactive", "--extend", "3"], rm_cls)
    assert result.exit_code == 0, result.output
    # Lookup with the short prefix, but extend with the full id.
    mgr.get_connection_info.assert_called_once_with("abc12345", UID)
    mgr.extend_reservation.assert_called_once_with(full, UID, 3.0)


# --------------------------------------------------------------------------- #
# --extend                                                                     #
# --------------------------------------------------------------------------- #
def test_extend_zero_rejected(cli_runner):
    rm_cls, mgr = _patch_mgr()
    result = _invoke(cli_runner, ["edit", RID, "--no-interactive", "--extend", "0"], rm_cls)
    assert result.exit_code == 0, result.output
    assert "Extension hours must be positive" in result.output
    mgr.extend_reservation.assert_not_called()


def test_extend_negative_rejected(cli_runner):
    rm_cls, mgr = _patch_mgr()
    result = _invoke(cli_runner, ["edit", RID, "--no-interactive", "--extend", "-5"], rm_cls)
    assert result.exit_code == 0, result.output
    assert "Extension hours must be positive" in result.output
    mgr.extend_reservation.assert_not_called()


def test_extend_above_max_rejected(cli_runner):
    rm_cls, mgr = _patch_mgr()
    result = _invoke(cli_runner, ["edit", RID, "--no-interactive", "--extend", "25"], rm_cls)
    assert result.exit_code == 0, result.output
    # rich highlights the "24" number as a separate span; match the stable parts.
    assert "Maximum extension is" in result.output
    assert "hours" in result.output
    mgr.extend_reservation.assert_not_called()


def test_extend_exactly_24_allowed(cli_runner):
    """24 is the boundary: extend > 24 is rejected, so 24 must be accepted."""
    rm_cls, mgr = _patch_mgr(extend_reservation=True)
    result = _invoke(cli_runner, ["edit", RID, "--no-interactive", "--extend", "24"], rm_cls)
    assert result.exit_code == 0, result.output
    mgr.extend_reservation.assert_called_once_with(RID, UID, 24.0)
    assert "Extended reservation" in result.output
    assert RID in result.output


def test_extend_success_message(cli_runner):
    rm_cls, mgr = _patch_mgr(extend_reservation=True)
    result = _invoke(cli_runner, ["edit", RID, "--no-interactive", "--extend", "8"], rm_cls)
    assert result.exit_code == 0, result.output
    mgr.extend_reservation.assert_called_once_with(RID, UID, 8.0)
    # rich splits the id + the "8.0" into separate styled spans; check the parts.
    assert "Extended reservation" in result.output
    assert RID in result.output
    assert "8.0" in result.output
    assert "hours" in result.output


def test_extend_fractional_hours(cli_runner):
    """Float hours (e.g. 0.5) flow through unrounded."""
    rm_cls, mgr = _patch_mgr(extend_reservation=True)
    result = _invoke(cli_runner, ["edit", RID, "--no-interactive", "--extend", "0.5"], rm_cls)
    assert result.exit_code == 0, result.output
    mgr.extend_reservation.assert_called_once_with(RID, UID, 0.5)


def test_extend_failure_message(cli_runner):
    rm_cls, mgr = _patch_mgr(extend_reservation=False)
    result = _invoke(cli_runner, ["edit", RID, "--no-interactive", "--extend", "4"], rm_cls)
    assert result.exit_code == 0, result.output
    mgr.extend_reservation.assert_called_once_with(RID, UID, 4.0)
    assert "Failed to extend reservation" in result.output
    assert RID in result.output


def test_extend_returns_before_jupyter_or_adduser(cli_runner):
    """When --extend is given alongside --add-user, the extend branch returns early
    and add_user is never invoked (extend is handled first and returns)."""
    rm_cls, mgr = _patch_mgr(extend_reservation=True)
    result = _invoke(
        cli_runner,
        ["edit", RID, "--no-interactive", "--extend", "2", "--add-user", "friend"],
        rm_cls,
    )
    assert result.exit_code == 0, result.output
    mgr.extend_reservation.assert_called_once()
    mgr.add_user.assert_not_called()


# --------------------------------------------------------------------------- #
# --add-user                                                                   #
# --------------------------------------------------------------------------- #
def test_add_user_success(cli_runner):
    rm_cls, mgr = _patch_mgr(add_user=True)
    result = _invoke(cli_runner, ["edit", RID, "--no-interactive", "--add-user", "friend"], rm_cls)
    assert result.exit_code == 0, result.output
    mgr.add_user.assert_called_once_with(RID, UID, "friend")
    assert f"User friend added to reservation {RID[:8]}" in result.output
    # The two follow-up hints reference the added user + the short id.
    assert "friend can now SSH" in result.output
    assert f"gpu-dev get-ssh-config {RID[:8]}" in result.output


def test_add_user_failure(cli_runner):
    rm_cls, mgr = _patch_mgr(add_user=False)
    result = _invoke(cli_runner, ["edit", RID, "--no-interactive", "--add-user", "friend"], rm_cls)
    assert result.exit_code == 0, result.output
    mgr.add_user.assert_called_once_with(RID, UID, "friend")
    assert "Failed to add user friend" in result.output
    # No success hints on failure.
    assert "can now SSH" not in result.output


# --------------------------------------------------------------------------- #
# --enable-jupyter / --disable-jupyter                                         #
# --------------------------------------------------------------------------- #
def test_enable_jupyter_success(cli_runner):
    rm_cls, mgr = _patch_mgr(enable_jupyter=True)
    result = _invoke(cli_runner, ["edit", RID, "--no-interactive", "--enable-jupyter"], rm_cls)
    assert result.exit_code == 0, result.output
    mgr.enable_jupyter.assert_called_once_with(RID, UID)
    mgr.disable_jupyter.assert_not_called()
    assert f"Jupyter Lab enabled for reservation {RID[:8]}" in result.output


def test_enable_jupyter_failure(cli_runner):
    rm_cls, mgr = _patch_mgr(enable_jupyter=False)
    result = _invoke(cli_runner, ["edit", RID, "--no-interactive", "--enable-jupyter"], rm_cls)
    assert result.exit_code == 0, result.output
    mgr.enable_jupyter.assert_called_once_with(RID, UID)
    assert "Failed to enable Jupyter Lab" in result.output


def test_disable_jupyter_success(cli_runner):
    rm_cls, mgr = _patch_mgr(disable_jupyter=True)
    result = _invoke(cli_runner, ["edit", RID, "--no-interactive", "--disable-jupyter"], rm_cls)
    assert result.exit_code == 0, result.output
    mgr.disable_jupyter.assert_called_once_with(RID, UID)
    mgr.enable_jupyter.assert_not_called()
    assert f"Jupyter Lab disabled for reservation {RID[:8]}" in result.output


def test_disable_jupyter_failure(cli_runner):
    rm_cls, mgr = _patch_mgr(disable_jupyter=False)
    result = _invoke(cli_runner, ["edit", RID, "--no-interactive", "--disable-jupyter"], rm_cls)
    assert result.exit_code == 0, result.output
    mgr.disable_jupyter.assert_called_once_with(RID, UID)
    assert "Failed to disable Jupyter Lab" in result.output


# --------------------------------------------------------------------------- #
# Exception handling                                                           #
# --------------------------------------------------------------------------- #
def test_exception_during_lookup_caught(cli_runner):
    """An exception from get_connection_info is caught by the top-level handler."""
    rm_cls, mgr = _patch_mgr()
    mgr.get_connection_info.side_effect = RuntimeError("ddb exploded")
    result = _invoke(cli_runner, ["edit", RID, "--no-interactive", "--extend", "2"], rm_cls)
    assert result.exit_code == 0, result.output
    assert "Error editing reservation" in result.output
    assert "ddb exploded" in result.output


def test_exception_during_extend_caught(cli_runner):
    rm_cls, mgr = _patch_mgr()
    mgr.extend_reservation.side_effect = ValueError("boom")
    result = _invoke(cli_runner, ["edit", RID, "--no-interactive", "--extend", "2"], rm_cls)
    assert result.exit_code == 0, result.output
    assert "Error editing reservation" in result.output
    assert "boom" in result.output


# --------------------------------------------------------------------------- #
# --extend type coercion (click validates the float)                          #
# --------------------------------------------------------------------------- #
def test_extend_non_numeric_rejected_by_click(cli_runner):
    """--extend is a float option; a non-numeric value fails click validation
    with a non-zero exit code before the command body runs."""
    rm_cls, mgr = _patch_mgr()
    result = _invoke(cli_runner, ["edit", RID, "--no-interactive", "--extend", "lots"], rm_cls)
    assert result.exit_code != 0
    mgr.extend_reservation.assert_not_called()
