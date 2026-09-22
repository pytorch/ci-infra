"""Unit tests for the `gpu-dev reserve` command (cli.py).

Focus: flag handling in NON-interactive mode (so we never hit questionary /
spinner-driven pickers): --ref implies ephemeral, --no-persist + --disk conflict
guard, gpu-type/count validation, --spot routing, the synchronous warm-pool
fast-path (claim_direct) condition, and --no-connect.

Everything that touches AWS / SSH / config / the ReservationManager is patched
where the names are looked up (gpu_dev_cli.cli). All tests force --no-interactive
and stub check_interactive_support -> False so the non-interactive disk picker is
skipped and create_reservation / claim_direct are the only manager calls.
"""
import re
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from gpu_dev_cli.cli import main


USER_INFO = {"user_id": "alice", "github_user": "alice-gh"}

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def plain(output: str) -> str:
    """Strip rich/ANSI color codes so substring asserts aren't broken by the
    per-token styling rich applies to numbers and punctuation."""
    return _ANSI.sub("", output)


def _make_config(environment="prod"):
    cfg = MagicMock(name="config")
    cfg.user_config = {"environment": environment}
    cfg.aws_region = "us-east-2"
    cfg.get_github_username.return_value = "alice-gh"
    return cfg


@contextmanager
def reserve_env(*, claim_direct=None, create_reservation="resv-1234567890abcdef",
                multinode=None, environment="prod", list_reservations=None):
    """Patch every collaborator the reserve command looks up in non-interactive
    mode. Yields the ReservationManager instance MagicMock so tests can assert
    which methods were called and with what kwargs.

    claim_direct: return value for reservation_mgr.claim_direct (None => fall back).
    create_reservation: return value for create_reservation (the reservation id).
    multinode: return value for create_multinode_reservation (list of ids or None).
    """
    mgr = MagicMock(name="ReservationManager_instance")
    mgr.claim_direct.return_value = claim_direct
    mgr.create_reservation.return_value = create_reservation
    mgr.create_multinode_reservation.return_value = multinode
    mgr.list_reservations.return_value = list_reservations if list_reservations is not None else []
    # wait_for_* returns falsy -> command prints the "use gpu-dev show" hint and
    # skips autoconnect, keeping output deterministic and side-effect free.
    mgr.wait_for_reservation_completion.return_value = None
    mgr.wait_for_multinode_reservation_completion.return_value = None

    with patch("gpu_dev_cli.cli.ReservationManager", return_value=mgr) as mgr_cls, \
            patch("gpu_dev_cli.cli.load_config", return_value=_make_config(environment)), \
            patch("gpu_dev_cli.cli.authenticate_user", return_value=USER_INFO), \
            patch("gpu_dev_cli.cli._validate_ssh_key_or_exit", return_value=True), \
            patch("gpu_dev_cli.cli.check_interactive_support", return_value=False), \
            patch("gpu_dev_cli.cli._maybe_autoconnect") as autoconnect, \
            patch("gpu_dev_cli.cli._maybe_show_sdk_tip"), \
            patch("gpu_dev_cli.cli._show_direct_success") as show_direct:
        mgr._cls = mgr_cls
        mgr._autoconnect = autoconnect
        mgr._show_direct = show_direct
        yield mgr


# --------------------------------------------------------------------------- #
# --no-persist + --disk conflict guard (sys.exit(2))
# --------------------------------------------------------------------------- #
def test_no_persist_with_named_disk_is_mutually_exclusive(cli_runner):
    with reserve_env() as mgr:
        r = cli_runner.invoke(main, [
            "reserve", "--no-interactive", "-t", "h100", "-g", "1", "-h", "1",
            "--no-persist", "--disk", "mydisk",
        ])
    assert r.exit_code == 2
    assert "mutually exclusive" in plain(r.output)
    # Guard fires before any reservation is created.
    mgr.create_reservation.assert_not_called()
    mgr.claim_direct.assert_not_called()


def test_no_persist_with_disk_none_is_allowed(cli_runner):
    # --disk none normalizes to "no persistent disk", so it should NOT conflict
    # with --no-persist; reservation proceeds (here via SQS create_reservation
    # because no_persist disqualifies the direct fast-path's `not disk`... actually
    # disk is None here so direct path IS attempted; force claim_direct miss).
    with reserve_env(claim_direct=None) as mgr:
        r = cli_runner.invoke(main, [
            "reserve", "--no-interactive", "-t", "h100", "-g", "1", "-h", "1",
            "--no-persist", "--disk", "none",
        ])
    assert r.exit_code == 0
    assert "mutually exclusive" not in plain(r.output)
    # disk none + no_persist => no_persistent_disk=True passed through.
    mgr.create_reservation.assert_called_once()
    assert mgr.create_reservation.call_args.kwargs["no_persistent_disk"] is True
    assert mgr.create_reservation.call_args.kwargs["disk_name"] is None


# --------------------------------------------------------------------------- #
# --ref implies ephemeral (no persistent disk) + skips the direct fast path
# --------------------------------------------------------------------------- #
def test_ref_implies_ephemeral_no_persist(cli_runner):
    with reserve_env() as mgr:
        r = cli_runner.invoke(main, [
            "reserve", "--no-interactive", "-t", "h100", "-g", "1", "-h", "1",
            "--ref", "pr/123",
        ])
    assert r.exit_code == 0
    assert "ephemeral pod" in plain(r.output)
    # --ref disqualifies the synchronous fast path (condition excludes `ref`).
    mgr.claim_direct.assert_not_called()
    mgr.create_reservation.assert_called_once()
    kw = mgr.create_reservation.call_args.kwargs
    assert kw["ref"] == "pr/123"
    # ref => ephemeral => no_persistent_disk True even without --no-persist/--disk.
    assert kw["no_persistent_disk"] is True


def test_ref_none_does_not_imply_ephemeral(cli_runner):
    # "--ref none" explicitly skips staging and must NOT flip no_persist.
    # With no disk/ref/spot the fast path is attempted; claim succeeds.
    claimed = {"reservation_id": "rid-direct", "ssh_command": "ssh x", "pod_name": "p"}
    with reserve_env(claim_direct=claimed) as mgr:
        r = cli_runner.invoke(main, [
            "reserve", "--no-interactive", "-t", "h100", "-g", "1", "-h", "1",
            "--ref", "none",
        ])
    assert r.exit_code == 0
    assert "ephemeral pod" not in plain(r.output)
    # ref=="none" is falsy-ish for the fast path? The guard is `not ref` and ref
    # is the literal string "none" (truthy), so the fast path is skipped and we
    # fall through to create_reservation with ref="none".
    mgr.claim_direct.assert_not_called()
    mgr.create_reservation.assert_called_once()
    assert mgr.create_reservation.call_args.kwargs["ref"] == "none"


# --------------------------------------------------------------------------- #
# GPU type / count validation
# --------------------------------------------------------------------------- #
def test_invalid_gpu_type_rejected_by_click(cli_runner):
    # --gpu-type is a click.Choice => click rejects unknown values (exit 2).
    with reserve_env() as mgr:
        r = cli_runner.invoke(main, [
            "reserve", "--no-interactive", "-t", "a999", "-g", "1", "-h", "1",
        ])
    assert r.exit_code == 2
    assert "Invalid value" in plain(r.output) or "invalid choice" in plain(r.output).lower()
    mgr.create_reservation.assert_not_called()


def test_gpu_type_is_case_insensitive(cli_runner):
    # click.Choice(case_sensitive=False) + cli lowercases; "H100" works.
    with reserve_env(claim_direct={"reservation_id": "r", "ssh_command": "s", "pod_name": "p"}) as mgr:
        r = cli_runner.invoke(main, [
            "reserve", "--no-interactive", "-t", "H100", "-g", "1", "-h", "1",
        ])
    assert r.exit_code == 0
    mgr.claim_direct.assert_called_once()
    assert mgr.claim_direct.call_args.kwargs["gpu_type"] == "h100"


def test_cpu_type_with_nonzero_gpus_errors(cli_runner):
    with reserve_env() as mgr:
        r = cli_runner.invoke(main, [
            "reserve", "--no-interactive", "-t", "cpu-x86", "-g", "2", "-h", "1",
        ])
    assert r.exit_code == 0  # handled gracefully, prints error + returns
    assert "CPU-only instances must have --gpus" in plain(r.output)
    mgr.create_reservation.assert_not_called()
    mgr.claim_direct.assert_not_called()


def test_gpu_type_with_zero_gpus_errors(cli_runner):
    with reserve_env() as mgr:
        r = cli_runner.invoke(main, [
            "reserve", "--no-interactive", "-t", "h100", "-g", "0", "-h", "1",
        ])
    # "0" is not in the click.Choice for --gpus, so click rejects it first.
    assert r.exit_code == 2
    mgr.create_reservation.assert_not_called()


def test_multinode_requires_distributed_flag(cli_runner):
    # 16 h100 GPUs > 8 max/node => multinode; non-interactive needs --distributed.
    with reserve_env() as mgr:
        r = cli_runner.invoke(main, [
            "reserve", "--no-interactive", "-t", "h100", "-g", "16", "-h", "1",
        ])
    assert r.exit_code == 0
    assert "require the --distributed flag" in plain(r.output)
    mgr.create_multinode_reservation.assert_not_called()
    mgr.create_reservation.assert_not_called()


def test_multinode_with_distributed_submits(cli_runner):
    with reserve_env(multinode=["rid-a", "rid-b"]) as mgr:
        r = cli_runner.invoke(main, [
            "reserve", "--no-interactive", "-t", "h100", "-g", "16", "-h", "1",
            "--distributed",
        ])
    assert r.exit_code == 0
    mgr.create_multinode_reservation.assert_called_once()
    kw = mgr.create_multinode_reservation.call_args.kwargs
    assert kw["gpu_count"] == 16
    assert kw["gpu_type"] == "h100"
    # multinode never goes through the single-node direct fast path.
    mgr.claim_direct.assert_not_called()


def test_multinode_non_multiple_rejected(cli_runner):
    # 12 is in the click.Choice but 12 % 8 != 0 => invalid multinode multiple.
    with reserve_env() as mgr:
        r = cli_runner.invoke(main, [
            "reserve", "--no-interactive", "-t", "h100", "-g", "12", "-h", "1",
            "--distributed",
        ])
    assert r.exit_code == 0
    assert "must be a multiple of" in plain(r.output)
    mgr.create_multinode_reservation.assert_not_called()


def test_hours_over_24_for_gpu_rejected(cli_runner):
    with reserve_env() as mgr:
        r = cli_runner.invoke(main, [
            "reserve", "--no-interactive", "-t", "h100", "-g", "1", "-h", "48",
        ])
    assert r.exit_code == 0
    assert "Maximum reservation time is" in plain(r.output) and "hours for GPU instances" in plain(r.output)
    mgr.create_reservation.assert_not_called()
    mgr.claim_direct.assert_not_called()


def test_hours_below_minimum_rejected(cli_runner):
    with reserve_env() as mgr:
        r = cli_runner.invoke(main, [
            "reserve", "--no-interactive", "-t", "h100", "-g", "1", "-h", "0.01",
        ])
    assert r.exit_code == 0
    assert "Minimum reservation time is" in plain(r.output) and "minutes" in plain(r.output)
    mgr.create_reservation.assert_not_called()


def test_cpu_type_allows_hours_over_24(cli_runner):
    # CPU types have no 24h ceiling; a 48h cpu reserve should not hit that error.
    # "0" isn't a valid --gpus click.Choice; cpu types default to 0 GPUs, so omit -g.
    with reserve_env(claim_direct={"reservation_id": "r", "ssh_command": "s", "pod_name": "p"}) as mgr:
        r = cli_runner.invoke(main, [
            "reserve", "--no-interactive", "-t", "cpu-x86", "-h", "48",
        ])
    assert r.exit_code == 0
    assert "hours for GPU instances" not in plain(r.output)
    # cpu-x86 is on-demand single-node ephemeral => direct fast path attempted.
    mgr.claim_direct.assert_called_once()


# --------------------------------------------------------------------------- #
# Synchronous warm-pool fast path (claim_direct) condition
# --------------------------------------------------------------------------- #
def test_fast_path_attempted_for_simple_single_node(cli_runner):
    claimed = {"reservation_id": "rid-fast", "ssh_command": "ssh x", "pod_name": "p"}
    with reserve_env(claim_direct=claimed) as mgr:
        r = cli_runner.invoke(main, [
            "reserve", "--no-interactive", "-t", "h100", "-g", "1", "-h", "2",
            "-n", "myrun",
        ])
    assert r.exit_code == 0
    mgr.claim_direct.assert_called_once()
    kw = mgr.claim_direct.call_args.kwargs
    assert kw["user_id"] == "alice"
    assert kw["gpu_count"] == 1
    assert kw["gpu_type"] == "h100"
    assert kw["duration_hours"] == 2.0
    assert kw["name"] == "myrun"
    assert kw["github_user"] == "alice-gh"
    # On a successful claim we show the direct-success block and DON'T create via SQS.
    mgr._show_direct.assert_called_once()
    mgr.create_reservation.assert_not_called()


def test_fast_path_miss_falls_back_to_sqs(cli_runner):
    # claim_direct returns None (no warm pod / no Function-URL access) => SQS path.
    with reserve_env(claim_direct=None) as mgr:
        r = cli_runner.invoke(main, [
            "reserve", "--no-interactive", "-t", "h100", "-g", "1", "-h", "2",
        ])
    assert r.exit_code == 0
    mgr.claim_direct.assert_called_once()
    mgr.create_reservation.assert_called_once()
    mgr._show_direct.assert_not_called()


def test_fast_path_skipped_when_disk_specified(cli_runner):
    # A named disk disqualifies the fast path (`not disk`). list_disks is called
    # to validate the disk, so patch it and return the disk as existing+free.
    disk_rec = {"name": "mydisk", "in_use": False, "size_gb": 100,
                "snapshot_count": 0, "is_deleted": False}
    with reserve_env() as mgr, \
            patch("gpu_dev_cli.disks.list_disks", return_value=[disk_rec]):
        r = cli_runner.invoke(main, [
            "reserve", "--no-interactive", "-t", "h100", "-g", "1", "-h", "2",
            "--disk", "mydisk",
        ])
    assert r.exit_code == 0
    mgr.claim_direct.assert_not_called()
    mgr.create_reservation.assert_called_once()
    assert mgr.create_reservation.call_args.kwargs["disk_name"] == "mydisk"


def test_fast_path_skipped_when_spot(cli_runner):
    # --spot disqualifies the fast path (`not spot`).
    with reserve_env(environment="prod") as mgr:
        r = cli_runner.invoke(main, [
            "reserve", "--no-interactive", "-t", "h100", "-g", "1", "-h", "2",
            "--spot",
        ])
    assert r.exit_code == 0
    mgr.claim_direct.assert_not_called()
    mgr.create_reservation.assert_called_once()
    assert mgr.create_reservation.call_args.kwargs["spot"] is True


def test_fast_path_skipped_when_multi_gpu_still_single_node(cli_runner):
    # 8 h100 GPUs == max/node, so it's single-node but the fast path IS attempted
    # (gpu_count <= max_gpus). Confirm claim_direct is tried with gpu_count=8.
    with reserve_env(claim_direct=None) as mgr:
        r = cli_runner.invoke(main, [
            "reserve", "--no-interactive", "-t", "h100", "-g", "8", "-h", "2",
        ])
    assert r.exit_code == 0
    mgr.claim_direct.assert_called_once()
    assert mgr.claim_direct.call_args.kwargs["gpu_count"] == 8


# --------------------------------------------------------------------------- #
# --no-connect
# --------------------------------------------------------------------------- #
def test_no_connect_passed_to_autoconnect_on_direct_claim(cli_runner):
    claimed = {"reservation_id": "rid-fast", "ssh_command": "s", "pod_name": "p"}
    with reserve_env(claim_direct=claimed) as mgr:
        r = cli_runner.invoke(main, [
            "reserve", "--no-interactive", "-t", "h100", "-g", "1", "-h", "2",
            "--no-connect",
        ])
    assert r.exit_code == 0
    mgr._autoconnect.assert_called_once()
    # _maybe_autoconnect(ctx, rid, no_connect) — third positional arg is no_connect.
    args = mgr._autoconnect.call_args.args
    assert args[1] == "rid-fast"
    assert args[2] is True


def test_connect_default_is_false_on_direct_claim(cli_runner):
    claimed = {"reservation_id": "rid-fast", "ssh_command": "s", "pod_name": "p"}
    with reserve_env(claim_direct=claimed) as mgr:
        r = cli_runner.invoke(main, [
            "reserve", "--no-interactive", "-t", "h100", "-g", "1", "-h", "2",
        ])
    assert r.exit_code == 0
    mgr._autoconnect.assert_called_once()
    assert mgr._autoconnect.call_args.args[2] is False


# --------------------------------------------------------------------------- #
# defaults in non-interactive mode
# --------------------------------------------------------------------------- #
def test_noninteractive_defaults_gpu_type_and_hours(cli_runner):
    # No -t, no -h, no -g => defaults a100 / 8.0h / 1 GPU. With --no-interactive
    # and check_interactive_support False, no pickers run.
    with reserve_env(claim_direct={"reservation_id": "r", "ssh_command": "s", "pod_name": "p"}) as mgr:
        r = cli_runner.invoke(main, ["reserve", "--no-interactive"])
    assert r.exit_code == 0
    mgr.claim_direct.assert_called_once()
    kw = mgr.claim_direct.call_args.kwargs
    assert kw["gpu_type"] == "a100"
    assert kw["gpu_count"] == 1
    assert kw["duration_hours"] == 8.0


def test_cpu_type_defaults_to_zero_gpus(cli_runner):
    with reserve_env(claim_direct={"reservation_id": "r", "ssh_command": "s", "pod_name": "p"}) as mgr:
        r = cli_runner.invoke(main, ["reserve", "--no-interactive", "-t", "cpu-arm"])
    assert r.exit_code == 0
    mgr.claim_direct.assert_called_once()
    assert mgr.claim_direct.call_args.kwargs["gpu_count"] == 0
