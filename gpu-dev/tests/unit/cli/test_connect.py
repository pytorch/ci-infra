"""Unit tests for `gpu-dev connect` — focus on the SSH exit-255 status
distinction (expired vs cancelled vs dropped-connection vs genuine auth failure)
plus the surrounding guard branches (not found, not active, no ssh command).

All network / AWS / SSH is mocked. We patch the names where `connect` looks them
up (in ``gpu_dev_cli.cli``) and patch ``pathlib.Path.home`` to a tmp dir so the
real ``~/.gpu-dev/<id>-sshconfig`` fast-path never short-circuits the command.
"""
import subprocess
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from gpu_dev_cli.cli import main


PRIMARY = "alice"
OTHER = "bob"


@contextmanager
def _connect_env(
    tmp_path,
    *,
    user_id=PRIMARY,
    connection_info,
    ssh_returncode=0,
    fresh_info=None,
    fresh_raises=False,
):
    """Patch everything `connect` touches.

    - load_config()         -> Config-ish stub with .user_config (env=prod)
    - authenticate_user()   -> {"user_id": user_id, "github_user": user_id}
    - ReservationManager()  -> instance whose get_connection_info returns
                               `connection_info` first, then `fresh_info` (the
                               255 re-check). If `fresh_raises`, the re-check
                               raises (status stays "").
    - subprocess.run        -> a MagicMock returning the given returncode.
    - Path.home()           -> tmp_path (no ~/.gpu-dev fast-path).
    Yields (mgr_instance, run_mock).
    """
    cfg = MagicMock()
    cfg.user_config = {"environment": "prod"}

    mgr = MagicMock()
    if fresh_raises:
        mgr.get_connection_info.side_effect = [connection_info, RuntimeError("boom")]
    else:
        mgr.get_connection_info.side_effect = [connection_info, fresh_info or {}]

    run_mock = MagicMock(return_value=MagicMock(returncode=ssh_returncode))

    with patch("gpu_dev_cli.cli.load_config", return_value=cfg), patch(
        "gpu_dev_cli.cli.authenticate_user",
        return_value={"user_id": user_id, "github_user": user_id},
    ), patch("gpu_dev_cli.cli.ReservationManager", return_value=mgr), patch(
        "subprocess.run", run_mock
    ), patch(
        "pathlib.Path.home", return_value=tmp_path
    ):
        yield mgr, run_mock


def _conn(status="active", user_id=PRIMARY, ssh_command="ssh dev@pod.example", **extra):
    d = {
        "status": status,
        "user_id": user_id,
        "ssh_command": ssh_command,
        "reservation_id": "abc12345deadbeef",
        "pod_name": "gpu-dev-abc12345",
    }
    d.update(extra)
    return d


def _run(cli_runner, rid="abc12345"):
    return cli_runner.invoke(main, ["connect", rid])


# --------------------------------------------------------------------------- #
# 255 handling: expired / cancelled / dropped / auth                          #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("fresh_status", ["expired", "completed", "cancelled", "canceled", "ended", "failed"])
def test_255_expired_or_cancelled_says_pod_is_gone(cli_runner, tmp_path, fresh_status):
    """SSH exits 255 and the re-checked status is a terminal one -> 'pod is gone',
    NOT an auth-failure message (even for the primary user)."""
    with _connect_env(
        tmp_path,
        user_id=PRIMARY,
        connection_info=_conn(status="active", user_id=PRIMARY),
        ssh_returncode=255,
        fresh_info={"status": fresh_status},
    ) as (mgr, run_mock):
        r = _run(cli_runner)

    assert r.exit_code == 0
    assert fresh_status in r.output
    assert "the pod is gone" in r.output
    assert "gpu-dev reserve" in r.output
    # Must NOT blame auth / tell the owner to ask themselves for access.
    assert "Authentication failed" not in r.output
    assert "--add-user" not in r.output
    # SSH was actually attempted, and the status was re-checked afterwards.
    run_mock.assert_called_once()
    assert mgr.get_connection_info.call_count == 2


def test_255_owner_dropped_connection_not_auth(cli_runner, tmp_path):
    """Primary user, 255, but reservation is still active -> dropped connection,
    suggest reconnect, never an auth-failure message."""
    with _connect_env(
        tmp_path,
        user_id=PRIMARY,
        connection_info=_conn(status="active", user_id=PRIMARY),
        ssh_returncode=255,
        fresh_info={"status": "active"},
    ) as (mgr, run_mock):
        r = _run(cli_runner)

    assert r.exit_code == 0
    assert "Connection to reservation abc12345 closed" in r.output
    assert "reconnect" in r.output
    assert "Authentication failed" not in r.output
    assert "--add-user" not in r.output


def test_255_owner_status_unknown_still_treated_as_dropped(cli_runner, tmp_path):
    """Primary user, 255, fresh re-check throws (status stays '') -> the
    `current_user == primary_user` branch still wins -> dropped, not auth."""
    with _connect_env(
        tmp_path,
        user_id=PRIMARY,
        connection_info=_conn(status="active", user_id=PRIMARY),
        ssh_returncode=255,
        fresh_raises=True,
    ) as (mgr, run_mock):
        r = _run(cli_runner)

    assert r.exit_code == 0
    assert "closed" in r.output
    assert "Authentication failed" not in r.output


def test_255_non_owner_genuine_auth_failure(cli_runner, tmp_path):
    """A different user (not on the reservation), 255, status not active/terminal
    -> genuine auth failure: tell them to ask the PRIMARY user to add them."""
    with _connect_env(
        tmp_path,
        user_id=OTHER,
        connection_info=_conn(status="active", user_id=PRIMARY),
        ssh_returncode=255,
        fresh_info={"status": "preparing"},  # not active, not terminal
    ) as (mgr, run_mock):
        r = _run(cli_runner)

    assert r.exit_code == 0
    assert "Authentication failed" in r.output
    assert PRIMARY in r.output  # ask the primary user
    assert f"--add-user {OTHER}" in r.output
    assert "get-ssh-config abc12345" in r.output
    # Must NOT be misclassified as a dropped/expired connection.
    assert "the pod is gone" not in r.output
    assert "closed" not in r.output


def test_255_non_owner_but_status_active_is_dropped_not_auth(cli_runner, tmp_path):
    """Non-owner whose fresh status comes back 'active' -> the `status == active`
    branch wins -> dropped connection, NOT auth failure."""
    with _connect_env(
        tmp_path,
        user_id=OTHER,
        connection_info=_conn(status="active", user_id=PRIMARY),
        ssh_returncode=255,
        fresh_info={"status": "active"},
    ) as (mgr, run_mock):
        r = _run(cli_runner)

    assert r.exit_code == 0
    assert "closed" in r.output
    assert "Authentication failed" not in r.output


def test_255_non_owner_terminal_status_is_pod_gone_not_auth(cli_runner, tmp_path):
    """Terminal status takes precedence over the auth branch even for a non-owner."""
    with _connect_env(
        tmp_path,
        user_id=OTHER,
        connection_info=_conn(status="active", user_id=PRIMARY),
        ssh_returncode=255,
        fresh_info={"status": "expired"},
    ) as (mgr, run_mock):
        r = _run(cli_runner)

    assert r.exit_code == 0
    assert "expired" in r.output
    assert "the pod is gone" in r.output
    assert "Authentication failed" not in r.output


def test_255_missing_user_id_defaults_to_primary_placeholder(cli_runner, tmp_path):
    """connection_info without user_id -> primary defaults to 'the-primary-user';
    a non-matching current user with non-terminal status -> auth failure naming
    that placeholder."""
    conn = _conn(status="active")
    conn.pop("user_id")
    with _connect_env(
        tmp_path,
        user_id=OTHER,
        connection_info=conn,
        ssh_returncode=255,
        fresh_info={"status": "preparing"},
    ) as (mgr, run_mock):
        r = _run(cli_runner)

    assert r.exit_code == 0
    assert "Authentication failed" in r.output
    assert "the-primary-user" in r.output


# --------------------------------------------------------------------------- #
# Non-255 paths                                                               #
# --------------------------------------------------------------------------- #

def test_successful_connect_no_255_block(cli_runner, tmp_path):
    """SSH returns 0 -> none of the 255 messaging fires, and the re-check
    get_connection_info is NOT called a second time."""
    with _connect_env(
        tmp_path,
        user_id=PRIMARY,
        connection_info=_conn(status="active", user_id=PRIMARY),
        ssh_returncode=0,
    ) as (mgr, run_mock):
        r = _run(cli_runner)

    assert r.exit_code == 0
    assert "Authentication failed" not in r.output
    assert "the pod is gone" not in r.output
    assert "closed" not in r.output
    run_mock.assert_called_once()
    assert mgr.get_connection_info.call_count == 1  # no 255 re-check


def test_non_255_nonzero_returncode_no_status_messaging(cli_runner, tmp_path):
    """A non-255 nonzero exit (e.g. the remote command failed) is left alone —
    no expired/dropped/auth messaging."""
    with _connect_env(
        tmp_path,
        user_id=PRIMARY,
        connection_info=_conn(status="active", user_id=PRIMARY),
        ssh_returncode=1,
    ) as (mgr, run_mock):
        r = _run(cli_runner)

    assert r.exit_code == 0
    assert "Authentication failed" not in r.output
    assert "the pod is gone" not in r.output
    assert "closed" not in r.output
    assert mgr.get_connection_info.call_count == 1


def test_ssh_command_gets_agent_forwarding_and_keys(cli_runner, tmp_path):
    """The ssh_command is augmented with -A and AddKeysToAgent before exec."""
    with _connect_env(
        tmp_path,
        user_id=PRIMARY,
        connection_info=_conn(status="active", user_id=PRIMARY, ssh_command="ssh dev@pod"),
        ssh_returncode=0,
    ) as (mgr, run_mock):
        r = _run(cli_runner)

    assert r.exit_code == 0
    executed = run_mock.call_args.args[0]
    assert executed.startswith("ssh ")
    assert " -A " in executed
    assert "AddKeysToAgent=yes" in executed
    assert "dev@pod" in executed


# --------------------------------------------------------------------------- #
# Guard branches before SSH runs                                              #
# --------------------------------------------------------------------------- #

def test_connection_info_none_errors_out(cli_runner, tmp_path):
    """No connection info found in either region -> error, never run ssh."""
    cfg = MagicMock()
    cfg.user_config = {"environment": "prod"}
    mgr = MagicMock()
    mgr.get_connection_info.return_value = None
    run_mock = MagicMock(return_value=MagicMock(returncode=0))

    with patch("gpu_dev_cli.cli.load_config", return_value=cfg), patch(
        "gpu_dev_cli.cli.authenticate_user",
        return_value={"user_id": PRIMARY, "github_user": PRIMARY},
    ), patch("gpu_dev_cli.cli.ReservationManager", return_value=mgr), patch(
        "subprocess.run", run_mock
    ), patch("pathlib.Path.home", return_value=tmp_path):
        r = _run(cli_runner)

    assert r.exit_code == 0
    assert "Could not get connection info" in r.output
    run_mock.assert_not_called()


def test_status_not_active_errors_out(cli_runner, tmp_path):
    """Reservation found but status != active -> reports status, never run ssh."""
    with _connect_env(
        tmp_path,
        user_id=PRIMARY,
        connection_info=_conn(status="queued", user_id=PRIMARY),
        ssh_returncode=0,
    ) as (mgr, run_mock):
        r = _run(cli_runner)

    assert r.exit_code == 0
    assert "not active" in r.output
    assert "queued" in r.output
    run_mock.assert_not_called()


def test_no_ssh_command_errors_out(cli_runner, tmp_path):
    """Active reservation with empty ssh_command -> error, never run ssh."""
    with _connect_env(
        tmp_path,
        user_id=PRIMARY,
        connection_info=_conn(status="active", user_id=PRIMARY, ssh_command=""),
        ssh_returncode=0,
    ) as (mgr, run_mock):
        r = _run(cli_runner)

    assert r.exit_code == 0
    assert "No SSH command available" in r.output
    run_mock.assert_not_called()


def test_auth_failure_during_setup_is_handled(cli_runner, tmp_path):
    """authenticate_user raising RuntimeError -> graceful error, no ssh, no crash."""
    cfg = MagicMock()
    cfg.user_config = {"environment": "prod"}
    run_mock = MagicMock(return_value=MagicMock(returncode=0))

    with patch("gpu_dev_cli.cli.load_config", return_value=cfg), patch(
        "gpu_dev_cli.cli.authenticate_user",
        side_effect=RuntimeError("no github key configured"),
    ), patch("gpu_dev_cli.cli.ReservationManager", return_value=MagicMock()), patch(
        "subprocess.run", run_mock
    ), patch("pathlib.Path.home", return_value=tmp_path):
        r = _run(cli_runner)

    assert r.exit_code == 0
    assert "no github key configured" in r.output
    run_mock.assert_not_called()


def test_keyboard_interrupt_during_ssh_is_caught(cli_runner, tmp_path):
    """Ctrl-C while connected -> 'Connection cancelled by user', no traceback."""
    cfg = MagicMock()
    cfg.user_config = {"environment": "prod"}
    mgr = MagicMock()
    mgr.get_connection_info.return_value = _conn(status="active", user_id=PRIMARY)

    with patch("gpu_dev_cli.cli.load_config", return_value=cfg), patch(
        "gpu_dev_cli.cli.authenticate_user",
        return_value={"user_id": PRIMARY, "github_user": PRIMARY},
    ), patch("gpu_dev_cli.cli.ReservationManager", return_value=mgr), patch(
        "subprocess.run", side_effect=KeyboardInterrupt
    ), patch("pathlib.Path.home", return_value=tmp_path):
        r = _run(cli_runner)

    assert r.exit_code == 0
    assert "Connection cancelled by user" in r.output
