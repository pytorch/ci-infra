"""Unit tests for `gpu-dev cancel`, focused on the in-pod fast-path.

The fast-path: when GPU_DEV_RESERVATION_ID + GPU_DEV_USER_ID are set (and the
user id is not the sentinel "warm"), `gpu-dev cancel` with no id and no --all
cancels THIS reservation directly via ReservationManager.cancel_reservation(rid,
uid), printing the "shutting down" message and skipping the interactive/auth
path. When those env vars are absent (or warm, or an explicit id/--all is
given), it falls back to the normal interactive / --all flow.

We patch the symbols where the cancel command looks them up: in
``gpu_dev_cli.cli`` (ReservationManager, load_config, authenticate_user,
check_interactive_support, remove_ssh_config_for_reservation).
"""
from unittest.mock import MagicMock, patch

import pytest

from gpu_dev_cli.cli import main

SHUTTING_DOWN = "Shutting down this reservation"
POD_RID = "5e83bb5b-aaaa-bbbb-cccc-1234567890ab"
POD_UID = "octocat"


@pytest.fixture
def clean_pod_env(monkeypatch):
    """Ensure the pod env vars start unset; tests opt in by setting them."""
    monkeypatch.delenv("GPU_DEV_RESERVATION_ID", raising=False)
    monkeypatch.delenv("GPU_DEV_USER_ID", raising=False)


def _patch_mgr(success=True):
    """Patch ReservationManager + load_config in cli; return (patch_ctx, mgr).

    The returned context manager patches both names; the mgr MagicMock's
    cancel_reservation returns ``success``.
    """
    mgr = MagicMock(name="ReservationManager_instance")
    mgr.cancel_reservation.return_value = success
    rm_cls = MagicMock(name="ReservationManager", return_value=mgr)
    return rm_cls, mgr


# --------------------------------------------------------------------------- #
# In-pod fast-path: env set, no id, no --all -> direct cancel                  #
# --------------------------------------------------------------------------- #
def test_in_pod_fast_path_cancels_directly(cli_runner, clean_pod_env, monkeypatch):
    monkeypatch.setenv("GPU_DEV_RESERVATION_ID", POD_RID)
    monkeypatch.setenv("GPU_DEV_USER_ID", POD_UID)
    rm_cls, mgr = _patch_mgr(success=True)

    with patch("gpu_dev_cli.cli.ReservationManager", rm_cls), \
         patch("gpu_dev_cli.cli.load_config", return_value=MagicMock(name="config")) as lc, \
         patch("gpu_dev_cli.cli.authenticate_user") as auth, \
         patch("gpu_dev_cli.cli.check_interactive_support") as cis:
        result = cli_runner.invoke(main, ["cancel"])

    assert result.exit_code == 0, result.output
    # Cancelled THIS reservation with the pod's rid + uid (not a truncated id).
    mgr.cancel_reservation.assert_called_once_with(POD_RID, POD_UID)
    # config was loaded for the manager, but auth + interactive were skipped.
    lc.assert_called_once()
    auth.assert_not_called()
    cis.assert_not_called()
    assert SHUTTING_DOWN in result.output


def test_in_pod_fast_path_does_not_truncate_rid(cli_runner, clean_pod_env, monkeypatch):
    """The pod fast-path must pass the FULL reservation id, not rid[:8]."""
    monkeypatch.setenv("GPU_DEV_RESERVATION_ID", POD_RID)
    monkeypatch.setenv("GPU_DEV_USER_ID", POD_UID)
    rm_cls, mgr = _patch_mgr(success=True)

    with patch("gpu_dev_cli.cli.ReservationManager", rm_cls), \
         patch("gpu_dev_cli.cli.load_config", return_value=MagicMock()):
        result = cli_runner.invoke(main, ["cancel"])

    assert result.exit_code == 0, result.output
    (passed_rid, passed_uid), _ = mgr.cancel_reservation.call_args
    assert passed_rid == POD_RID
    assert len(passed_rid) > 8


def test_in_pod_fast_path_failure_prints_laptop_hint(cli_runner, clean_pod_env, monkeypatch):
    """If the direct cancel returns False, print the laptop fallback hint with rid[:8]."""
    monkeypatch.setenv("GPU_DEV_RESERVATION_ID", POD_RID)
    monkeypatch.setenv("GPU_DEV_USER_ID", POD_UID)
    rm_cls, mgr = _patch_mgr(success=False)

    with patch("gpu_dev_cli.cli.ReservationManager", rm_cls), \
         patch("gpu_dev_cli.cli.load_config", return_value=MagicMock()):
        result = cli_runner.invoke(main, ["cancel"])

    assert result.exit_code == 0, result.output
    mgr.cancel_reservation.assert_called_once_with(POD_RID, POD_UID)
    # Shutting-down message still printed (it precedes the call), plus the hint.
    assert SHUTTING_DOWN in result.output
    assert f"gpu-dev cancel {POD_RID[:8]}" in result.output


def test_in_pod_fast_path_exception_caught(cli_runner, clean_pod_env, monkeypatch):
    """An exception building the manager / cancelling is caught -> hint, no crash."""
    monkeypatch.setenv("GPU_DEV_RESERVATION_ID", POD_RID)
    monkeypatch.setenv("GPU_DEV_USER_ID", POD_UID)
    rm_cls = MagicMock(side_effect=RuntimeError("boom"))

    with patch("gpu_dev_cli.cli.ReservationManager", rm_cls), \
         patch("gpu_dev_cli.cli.load_config", return_value=MagicMock()):
        result = cli_runner.invoke(main, ["cancel"])

    assert result.exit_code == 0, result.output
    assert "Could not cancel from inside the pod" in result.output
    assert "boom" in result.output
    # ok=False -> the laptop hint is also shown.
    assert f"gpu-dev cancel {POD_RID[:8]}" in result.output


# --------------------------------------------------------------------------- #
# Fast-path is NOT taken when conditions aren't met                            #
# --------------------------------------------------------------------------- #
def test_warm_uid_skips_fast_path(cli_runner, clean_pod_env, monkeypatch):
    """uid == 'warm' must NOT trigger the direct cancel; falls through to normal flow."""
    monkeypatch.setenv("GPU_DEV_RESERVATION_ID", POD_RID)
    monkeypatch.setenv("GPU_DEV_USER_ID", "warm")
    rm_cls, mgr = _patch_mgr(success=True)

    with patch("gpu_dev_cli.cli.ReservationManager", rm_cls), \
         patch("gpu_dev_cli.cli.load_config", return_value=MagicMock()), \
         patch("gpu_dev_cli.cli.authenticate_user",
               return_value={"user_id": "octocat"}) as auth, \
         patch("gpu_dev_cli.cli.check_interactive_support", return_value=False):
        result = cli_runner.invoke(main, ["cancel"])

    assert result.exit_code == 0, result.output
    # Fast-path message must NOT appear (warm path skips it).
    assert SHUTTING_DOWN not in result.output
    # Non-interactive (cis False) + no id -> "No reservation ID provided".
    assert "No reservation ID provided" in result.output
    mgr.cancel_reservation.assert_not_called()


def test_missing_uid_skips_fast_path(cli_runner, clean_pod_env, monkeypatch):
    """rid set but uid unset -> not the fast-path."""
    monkeypatch.setenv("GPU_DEV_RESERVATION_ID", POD_RID)
    # GPU_DEV_USER_ID intentionally unset (clean_pod_env removed it).
    rm_cls, mgr = _patch_mgr(success=True)

    with patch("gpu_dev_cli.cli.ReservationManager", rm_cls), \
         patch("gpu_dev_cli.cli.load_config", return_value=MagicMock()), \
         patch("gpu_dev_cli.cli.authenticate_user",
               return_value={"user_id": "octocat"}), \
         patch("gpu_dev_cli.cli.check_interactive_support", return_value=False):
        result = cli_runner.invoke(main, ["cancel"])

    assert result.exit_code == 0, result.output
    assert SHUTTING_DOWN not in result.output
    assert "No reservation ID provided" in result.output


def test_missing_rid_skips_fast_path(cli_runner, clean_pod_env, monkeypatch):
    """uid set but rid unset -> not the fast-path."""
    monkeypatch.setenv("GPU_DEV_USER_ID", POD_UID)
    rm_cls, mgr = _patch_mgr(success=True)

    with patch("gpu_dev_cli.cli.ReservationManager", rm_cls), \
         patch("gpu_dev_cli.cli.load_config", return_value=MagicMock()), \
         patch("gpu_dev_cli.cli.authenticate_user",
               return_value={"user_id": "octocat"}), \
         patch("gpu_dev_cli.cli.check_interactive_support", return_value=False):
        result = cli_runner.invoke(main, ["cancel"])

    assert result.exit_code == 0, result.output
    assert SHUTTING_DOWN not in result.output
    assert "No reservation ID provided" in result.output


def test_whitespace_only_env_skips_fast_path(cli_runner, clean_pod_env, monkeypatch):
    """Env vars that are whitespace get .strip()'d to empty -> not the fast-path."""
    monkeypatch.setenv("GPU_DEV_RESERVATION_ID", "   ")
    monkeypatch.setenv("GPU_DEV_USER_ID", "  ")
    rm_cls, mgr = _patch_mgr(success=True)

    with patch("gpu_dev_cli.cli.ReservationManager", rm_cls), \
         patch("gpu_dev_cli.cli.load_config", return_value=MagicMock()), \
         patch("gpu_dev_cli.cli.authenticate_user",
               return_value={"user_id": "octocat"}), \
         patch("gpu_dev_cli.cli.check_interactive_support", return_value=False):
        result = cli_runner.invoke(main, ["cancel"])

    assert result.exit_code == 0, result.output
    assert SHUTTING_DOWN not in result.output
    assert "No reservation ID provided" in result.output
    mgr.cancel_reservation.assert_not_called()


def test_explicit_id_skips_fast_path_even_in_pod(cli_runner, clean_pod_env, monkeypatch):
    """In-pod env set, but an explicit reservation_id arg -> normal (non-interactive) flow.

    The explicit id should be the one cancelled, with the AUTHENTICATED user id,
    not the pod uid; auth IS invoked because the fast-path is bypassed.
    """
    monkeypatch.setenv("GPU_DEV_RESERVATION_ID", POD_RID)
    monkeypatch.setenv("GPU_DEV_USER_ID", POD_UID)
    rm_cls, mgr = _patch_mgr(success=True)

    with patch("gpu_dev_cli.cli.ReservationManager", rm_cls), \
         patch("gpu_dev_cli.cli.load_config", return_value=MagicMock()), \
         patch("gpu_dev_cli.cli.authenticate_user",
               return_value={"user_id": "authed-user"}) as auth, \
         patch("gpu_dev_cli.cli.check_interactive_support", return_value=False), \
         patch("gpu_dev_cli.cli.remove_ssh_config_for_reservation") as rm_ssh:
        result = cli_runner.invoke(main, ["cancel", "deadbeef"])

    assert result.exit_code == 0, result.output
    auth.assert_called_once()
    mgr.cancel_reservation.assert_called_once_with("deadbeef", "authed-user")
    rm_ssh.assert_called_once_with("deadbeef")
    assert "cancelled" in result.output.lower()


def test_explicit_id_in_pod_prints_shutdown_after_success(cli_runner, clean_pod_env, monkeypatch):
    """Normal flow success + GPU_DEV_USER_ID set -> the post-cancel shutdown note prints."""
    monkeypatch.setenv("GPU_DEV_USER_ID", POD_UID)  # no rid -> not fast-path
    rm_cls, mgr = _patch_mgr(success=True)

    with patch("gpu_dev_cli.cli.ReservationManager", rm_cls), \
         patch("gpu_dev_cli.cli.load_config", return_value=MagicMock()), \
         patch("gpu_dev_cli.cli.authenticate_user",
               return_value={"user_id": "authed-user"}), \
         patch("gpu_dev_cli.cli.check_interactive_support", return_value=False), \
         patch("gpu_dev_cli.cli.remove_ssh_config_for_reservation"):
        result = cli_runner.invoke(main, ["cancel", "deadbeef"])

    assert result.exit_code == 0, result.output
    # Post-success shutdown hint (the second SHUTTING_DOWN site, gated on uid env).
    assert SHUTTING_DOWN in result.output


# --------------------------------------------------------------------------- #
# Conflicting options + --all bypasses fast-path                              #
# --------------------------------------------------------------------------- #
def test_all_and_id_conflict(cli_runner, clean_pod_env):
    rm_cls, mgr = _patch_mgr()
    with patch("gpu_dev_cli.cli.ReservationManager", rm_cls), \
         patch("gpu_dev_cli.cli.load_config", return_value=MagicMock()):
        result = cli_runner.invoke(main, ["cancel", "abc123", "--all"])

    assert result.exit_code == 0, result.output
    assert "Cannot specify both --all and a reservation ID" in result.output
    mgr.cancel_reservation.assert_not_called()


def test_all_flag_in_pod_skips_fast_path(cli_runner, clean_pod_env, monkeypatch):
    """--all with in-pod env set bypasses the fast-path and uses the --all flow."""
    monkeypatch.setenv("GPU_DEV_RESERVATION_ID", POD_RID)
    monkeypatch.setenv("GPU_DEV_USER_ID", POD_UID)
    rm_cls, mgr = _patch_mgr(success=True)

    with patch("gpu_dev_cli.cli.ReservationManager", rm_cls), \
         patch("gpu_dev_cli.cli.load_config", return_value=MagicMock()), \
         patch("gpu_dev_cli.cli.authenticate_user",
               return_value={"user_id": "octocat"}) as auth, \
         patch("gpu_dev_cli.cli._fetch_reservations_cross_region",
               return_value=[]) as fetch:
        result = cli_runner.invoke(main, ["cancel", "--all"])

    assert result.exit_code == 0, result.output
    # Fast-path skipped: its message must not appear; --all loaded reservations.
    assert SHUTTING_DOWN not in result.output
    auth.assert_called_once()
    fetch.assert_called_once()
    assert "No cancellable reservations found" in result.output


def test_all_flag_cancels_each(cli_runner, clean_pod_env, monkeypatch):
    """--force --all cancels every fetched reservation and reports the count."""
    reservations = [
        {"reservation_id": "id-one-11111111", "gpu_count": 1, "gpu_type": "h100",
         "status": "active", "created_at": "N/A"},
        {"reservation_id": "id-two-22222222", "gpu_count": 2, "gpu_type": "h100",
         "status": "queued", "created_at": "N/A"},
    ]
    rm_cls, mgr = _patch_mgr(success=True)

    with patch("gpu_dev_cli.cli.ReservationManager", rm_cls), \
         patch("gpu_dev_cli.cli.load_config", return_value=MagicMock()), \
         patch("gpu_dev_cli.cli.authenticate_user",
               return_value={"user_id": "octocat"}), \
         patch("gpu_dev_cli.cli._fetch_reservations_cross_region",
               return_value=reservations), \
         patch("gpu_dev_cli.cli.remove_ssh_config_for_reservation") as rm_ssh:
        result = cli_runner.invoke(main, ["cancel", "--all", "--force"])

    assert result.exit_code == 0, result.output
    assert mgr.cancel_reservation.call_count == 2
    mgr.cancel_reservation.assert_any_call("id-one-11111111", "octocat")
    mgr.cancel_reservation.assert_any_call("id-two-22222222", "octocat")
    assert rm_ssh.call_count == 2
    # rich styles the count separately; match the stable substrings.
    assert "Successfully cancelled" in result.output


def test_all_flag_no_reservations(cli_runner, clean_pod_env, monkeypatch):
    rm_cls, mgr = _patch_mgr()
    with patch("gpu_dev_cli.cli.ReservationManager", rm_cls), \
         patch("gpu_dev_cli.cli.load_config", return_value=MagicMock()), \
         patch("gpu_dev_cli.cli.authenticate_user",
               return_value={"user_id": "octocat"}), \
         patch("gpu_dev_cli.cli._fetch_reservations_cross_region", return_value=[]):
        result = cli_runner.invoke(main, ["cancel", "--all", "--force"])

    assert result.exit_code == 0, result.output
    assert "No cancellable reservations found" in result.output
    mgr.cancel_reservation.assert_not_called()


def test_all_flag_auth_failure(cli_runner, clean_pod_env, monkeypatch):
    """Auth RuntimeError in the --all flow is reported, no cancellation attempted."""
    rm_cls, mgr = _patch_mgr()
    with patch("gpu_dev_cli.cli.ReservationManager", rm_cls), \
         patch("gpu_dev_cli.cli.load_config", return_value=MagicMock()), \
         patch("gpu_dev_cli.cli.authenticate_user",
               side_effect=RuntimeError("no github_user configured")), \
         patch("gpu_dev_cli.cli._fetch_reservations_cross_region") as fetch:
        result = cli_runner.invoke(main, ["cancel", "--all"])

    assert result.exit_code == 0, result.output
    assert "no github_user configured" in result.output
    fetch.assert_not_called()
    mgr.cancel_reservation.assert_not_called()


# --------------------------------------------------------------------------- #
# Non-interactive normal flow (no pod env)                                     #
# --------------------------------------------------------------------------- #
def test_non_interactive_explicit_id_success(cli_runner, clean_pod_env):
    rm_cls, mgr = _patch_mgr(success=True)
    with patch("gpu_dev_cli.cli.ReservationManager", rm_cls), \
         patch("gpu_dev_cli.cli.load_config", return_value=MagicMock()), \
         patch("gpu_dev_cli.cli.authenticate_user",
               return_value={"user_id": "octocat"}), \
         patch("gpu_dev_cli.cli.check_interactive_support", return_value=False), \
         patch("gpu_dev_cli.cli.remove_ssh_config_for_reservation") as rm_ssh:
        result = cli_runner.invoke(main, ["cancel", "abcd1234extra"])

    assert result.exit_code == 0, result.output
    mgr.cancel_reservation.assert_called_once_with("abcd1234extra", "octocat")
    rm_ssh.assert_called_once_with("abcd1234extra")
    # Success message truncates the id to 8 chars.
    assert "Reservation abcd1234 cancelled" in result.output


def test_non_interactive_explicit_id_failure(cli_runner, clean_pod_env):
    rm_cls, mgr = _patch_mgr(success=False)
    with patch("gpu_dev_cli.cli.ReservationManager", rm_cls), \
         patch("gpu_dev_cli.cli.load_config", return_value=MagicMock()), \
         patch("gpu_dev_cli.cli.authenticate_user",
               return_value={"user_id": "octocat"}), \
         patch("gpu_dev_cli.cli.check_interactive_support", return_value=False), \
         patch("gpu_dev_cli.cli.remove_ssh_config_for_reservation") as rm_ssh:
        result = cli_runner.invoke(main, ["cancel", "abcd1234extra"])

    assert result.exit_code == 0, result.output
    assert "Failed to cancel reservation abcd1234" in result.output
    rm_ssh.assert_not_called()


def test_non_interactive_no_id_no_pod(cli_runner, clean_pod_env):
    """No pod env, no id, interactive disabled -> 'No reservation ID provided'."""
    rm_cls, mgr = _patch_mgr()
    with patch("gpu_dev_cli.cli.ReservationManager", rm_cls), \
         patch("gpu_dev_cli.cli.load_config", return_value=MagicMock()), \
         patch("gpu_dev_cli.cli.authenticate_user",
               return_value={"user_id": "octocat"}), \
         patch("gpu_dev_cli.cli.check_interactive_support", return_value=False):
        result = cli_runner.invoke(main, ["cancel", "--no-interactive"])

    assert result.exit_code == 0, result.output
    assert "No reservation ID provided" in result.output
    mgr.cancel_reservation.assert_not_called()
