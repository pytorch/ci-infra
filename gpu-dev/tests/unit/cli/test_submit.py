"""Unit tests for `gpu-dev submit` (cli-tools/gpu-dev-cli/gpu_dev_cli/cli.py).

Focus (per task): argument parsing, gpu-type choices, --spot flag,
--dockerfile/--dockerimage payloads, plus the success + failure arg-validation
branches. The ReservationManager / authenticate_user / load_config are mocked
where the cli module looks them up. No network / AWS / SSH / real subprocess.

Strategy for the "happy path" without driving the whole SSH flow: stub
``create_reservation`` (or ``create_multinode_reservation``) to return ``None``
so submit prints "Failed to create reservation" and ``sys.exit(2)`` right after
the reservation call — this lets us assert the exact kwargs passed to the
manager (gpu_type lowercasing, spot, dockerfile/dockerimage payload,
no_persistent_disk, disk_name, ref, source_command) without needing SSH.
"""
import base64
import os
import tarfile
from unittest.mock import MagicMock, patch

import pytest

from gpu_dev_cli.cli import main


USER_INFO = {"user_id": "u-123", "github_user": "octocat"}


# ---------------------------------------------------------------------------
# patch harness
# ---------------------------------------------------------------------------
def _patch_env(*, create_result=None, multinode_result=None, auth_raises=None):
    """Patch load_config / ReservationManager / authenticate_user.

    Defaults make create_reservation / create_multinode_reservation return None
    so the command exits 2 immediately after the reservation call (before any
    SSH/subprocess work), which is enough to assert the call kwargs.
    """
    rm = MagicMock(name="ReservationManager_instance")
    rm.create_reservation.return_value = create_result
    rm.create_multinode_reservation.return_value = multinode_result

    auth = MagicMock(name="authenticate_user")
    if auth_raises is not None:
        auth.side_effect = auth_raises
    else:
        auth.return_value = USER_INFO

    patches = [
        patch("gpu_dev_cli.cli.load_config", MagicMock(return_value=MagicMock())),
        patch("gpu_dev_cli.cli.ReservationManager", MagicMock(return_value=rm)),
        patch("gpu_dev_cli.cli.authenticate_user", auth),
    ]
    return patches, rm


class _Ctx:
    def __init__(self, patches, rm):
        self._patches = patches
        self.rm = rm

    def __enter__(self):
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in reversed(self._patches):
            p.stop()
        return False


def _run(cli_runner, args, **env):
    patches, rm = _patch_env(**env)
    with _Ctx(patches, rm):
        result = cli_runner.invoke(main, ["submit", *args])
    return result, rm


# ---------------------------------------------------------------------------
# command argument requirement (click-level)
# ---------------------------------------------------------------------------
def test_missing_command_is_usage_error(cli_runner):
    # COMMAND is nargs=-1 required=True -> click usage error, exit 2.
    res, rm = _run(cli_runner, [])
    assert res.exit_code == 2
    # never authenticated / reserved
    rm.create_reservation.assert_not_called()


def test_invalid_gpu_type_rejected_by_choice(cli_runner):
    # 'v100' is not in _SUBMIT_GPU_TYPES -> click.Choice rejects, exit 2.
    res, rm = _run(cli_runner, ["--gpu-type", "v100", "--", "nvidia-smi"])
    assert res.exit_code == 2
    assert "v100" in res.output
    rm.create_reservation.assert_not_called()


# ---------------------------------------------------------------------------
# the missing-'--' typo guard
# ---------------------------------------------------------------------------
def test_missing_double_dash_guard_triggers(cli_runner):
    # ignore_unknown_options means `gpus` would otherwise be run as a command.
    # The guard sees command[0] == 'gpus' and bails with exit 2.
    res, rm = _run(cli_runner, ["gpus", "1", "bash", "run.sh"])
    assert res.exit_code == 2
    assert "looks like a missing" in res.output
    assert "--gpus" in res.output
    rm.create_reservation.assert_not_called()


def test_missing_double_dash_guard_for_runtime(cli_runner):
    res, rm = _run(cli_runner, ["runtime", ".", "python", "x.py"])
    assert res.exit_code == 2
    assert "--runtime" in res.output


def test_normal_command_not_flagged_by_typo_guard(cli_runner):
    # 'python' is not a flag name -> guard does not fire; proceeds and fails at
    # create_reservation (returns None -> exit 2 with the reservation message).
    res, rm = _run(cli_runner, ["--", "python", "train.py"])
    assert res.exit_code == 2
    assert "Failed to create reservation" in res.output
    rm.create_reservation.assert_called_once()


# ---------------------------------------------------------------------------
# rsync presence check (only when --runtime given)
# ---------------------------------------------------------------------------
def test_runtime_without_rsync_aborts(cli_runner, tmp_path):
    rt = tmp_path / "rt"
    rt.mkdir()
    patches, rm = _patch_env()
    with _Ctx(patches, rm):
        with patch("shutil.which", MagicMock(return_value=None)):
            res = cli_runner.invoke(
                main, ["submit", "--runtime", str(rt), "--", "python", "x.py"])
    assert res.exit_code == 2
    assert "rsync not found" in res.output
    rm.create_reservation.assert_not_called()


def test_no_runtime_skips_rsync_check(cli_runner):
    # Without --runtime, shutil.which must never be consulted for rsync.
    patches, rm = _patch_env()
    with _Ctx(patches, rm):
        with patch("shutil.which", MagicMock(return_value=None)) as which:
            res = cli_runner.invoke(main, ["submit", "--", "nvidia-smi"])
    which.assert_not_called()
    # proceeds to (failing) reservation
    assert res.exit_code == 2
    assert "Failed to create reservation" in res.output


# ---------------------------------------------------------------------------
# gpu-type -> max_per_node lookup edge cases
# ---------------------------------------------------------------------------
def test_b300_choice_hits_unknown_gpu_type_branch(cli_runner):
    # b300 is a valid Choice but absent from max_per_node -> "Unknown gpu-type".
    # (Documented inconsistency; assert the real behavior, not a fix.)
    res, rm = _run(cli_runner, ["--gpu-type", "b300", "--", "nvidia-smi"])
    assert res.exit_code == 2
    assert "Unknown gpu-type" in res.output
    rm.create_reservation.assert_not_called()


def test_cpu_spot_choice_hits_unknown_gpu_type_branch(cli_runner):
    res, rm = _run(cli_runner, ["--gpu-type", "cpu-spot", "--", "echo", "hi"])
    assert res.exit_code == 2
    assert "Unknown gpu-type" in res.output


def test_gpu_type_is_case_insensitive_and_lowercased_in_call(cli_runner):
    # --gpu-type H100 (uppercase) accepted by case-insensitive Choice and passed
    # lowercase to the manager.
    res, rm = _run(cli_runner, ["--gpu-type", "H100", "--", "nvidia-smi"])
    assert res.exit_code == 2  # create_reservation returns None
    rm.create_reservation.assert_called_once()
    assert rm.create_reservation.call_args.kwargs["gpu_type"] == "h100"


# ---------------------------------------------------------------------------
# multinode detection + divisibility
# ---------------------------------------------------------------------------
def test_multinode_when_gpus_exceed_per_node(cli_runner):
    # 16 h100 GPUs (max 8/node) -> multinode path.
    res, rm = _run(
        cli_runner, ["--gpu-type", "h100", "--gpus", "16", "--", "bash", "r.sh"],
        multinode_result=None,
    )
    assert res.exit_code == 2
    assert "Failed to create multinode reservation" in res.output
    rm.create_multinode_reservation.assert_called_once()
    rm.create_reservation.assert_not_called()
    assert rm.create_multinode_reservation.call_args.kwargs["gpu_count"] == 16


def test_multinode_non_multiple_rejected(cli_runner):
    # 12 h100 (max 8) -> 12 % 8 != 0 -> error, exit 2, before any reservation.
    res, rm = _run(
        cli_runner, ["--gpu-type", "h100", "--gpus", "12", "--", "x"])
    assert res.exit_code == 2
    # rich styles the trailing number separately, so match the stable prefix.
    assert "must be a multiple of" in res.output
    rm.create_multinode_reservation.assert_not_called()
    rm.create_reservation.assert_not_called()


def test_gpus_equal_to_per_node_is_single_node(cli_runner):
    # 8 h100 == max_per_node -> NOT multinode (single create_reservation).
    res, rm = _run(
        cli_runner, ["--gpu-type", "h100", "--gpus", "8", "--", "x"])
    assert res.exit_code == 2
    rm.create_reservation.assert_called_once()
    rm.create_multinode_reservation.assert_not_called()


def test_cpu_type_never_multinode(cli_runner):
    # cpu-arm: max_per_node 0 but cpu types are excluded from multinode logic.
    res, rm = _run(
        cli_runner, ["--gpu-type", "cpu-arm", "--gpus", "4", "--", "echo", "hi"])
    assert res.exit_code == 2
    rm.create_reservation.assert_called_once()
    rm.create_multinode_reservation.assert_not_called()


# ---------------------------------------------------------------------------
# auth failure
# ---------------------------------------------------------------------------
def test_auth_failure_exits_two(cli_runner):
    res, rm = _run(
        cli_runner, ["--", "nvidia-smi"], auth_raises=RuntimeError("no keys"))
    assert res.exit_code == 2
    assert "no keys" in res.output
    rm.create_reservation.assert_not_called()


# ---------------------------------------------------------------------------
# --spot / --ref / --name / --hours / --no-persistent-disk / --disk threading
# ---------------------------------------------------------------------------
def test_spot_flag_threaded_to_create(cli_runner):
    res, rm = _run(cli_runner, ["--spot", "--", "nvidia-smi"])
    assert rm.create_reservation.call_args.kwargs["spot"] is True


def test_spot_defaults_false(cli_runner):
    res, rm = _run(cli_runner, ["--", "nvidia-smi"])
    assert rm.create_reservation.call_args.kwargs["spot"] is False


def test_ref_and_name_and_hours_threaded(cli_runner):
    res, rm = _run(
        cli_runner,
        ["--ref", "pr/123", "--name", "myjob", "--hours", "2.5", "--", "x"])
    kw = rm.create_reservation.call_args.kwargs
    assert kw["ref"] == "pr/123"
    assert kw["name"] == "myjob"
    assert kw["duration_hours"] == 2.5
    assert kw["source_command"] == "submit"
    assert kw["github_user"] == "octocat"
    assert kw["user_id"] == "u-123"


def test_no_persistent_disk_forces_disk_name_none(cli_runner):
    # --no-persistent-disk wins: disk_name passed as None even if --disk given.
    res, rm = _run(
        cli_runner, ["--no-persistent-disk", "--disk", "mydisk", "--", "x"])
    kw = rm.create_reservation.call_args.kwargs
    assert kw["no_persistent_disk"] is True
    assert kw["disk_name"] is None


def test_disk_name_passed_when_not_no_persist(cli_runner):
    res, rm = _run(cli_runner, ["--disk", "mydisk", "--", "x"])
    kw = rm.create_reservation.call_args.kwargs
    assert kw["no_persistent_disk"] is False
    assert kw["disk_name"] == "mydisk"


def test_default_disk_name_is_none(cli_runner):
    res, rm = _run(cli_runner, ["--", "x"])
    kw = rm.create_reservation.call_args.kwargs
    assert kw["disk_name"] is None
    assert kw["no_persistent_disk"] is False


# ---------------------------------------------------------------------------
# --dockerimage / --preserve-entrypoint
# ---------------------------------------------------------------------------
def test_dockerimage_threaded_and_note_printed(cli_runner):
    res, rm = _run(
        cli_runner, ["--dockerimage", "ghcr.io/me/img:tag", "--", "x"])
    kw = rm.create_reservation.call_args.kwargs
    assert kw["dockerimage"] == "ghcr.io/me/img:tag"
    assert kw["dockerfile"] is None
    assert kw["preserve_entrypoint"] is False
    # note about wrapping with SSH harness when no --preserve-entrypoint.
    # rich line-wraps the dim note, so match a stable single-line fragment.
    assert "without --preserve-entrypoint" in res.output


def test_dockerimage_with_preserve_entrypoint_no_note(cli_runner):
    res, rm = _run(
        cli_runner,
        ["--dockerimage", "ghcr.io/me/img:tag", "--preserve-entrypoint", "--", "x"])
    kw = rm.create_reservation.call_args.kwargs
    assert kw["preserve_entrypoint"] is True
    # with --preserve-entrypoint the wrapping note must NOT be printed.
    assert "without --preserve-entrypoint" not in res.output
    assert "Note: passing --dockerimage" not in res.output


def test_preserve_entrypoint_without_image_or_file_errors(cli_runner):
    res, rm = _run(cli_runner, ["--preserve-entrypoint", "--", "x"])
    assert res.exit_code == 2
    assert "--preserve-entrypoint requires --dockerfile or --dockerimage" in res.output
    rm.create_reservation.assert_not_called()


# ---------------------------------------------------------------------------
# --dockerfile payload building
# ---------------------------------------------------------------------------
def _write_dockerfile(tmp_path, name="Dockerfile", content=b"FROM scratch\n"):
    p = tmp_path / name
    p.write_bytes(content)
    return p


def test_dockerfile_payload_is_base64_targz(cli_runner, tmp_path):
    df = _write_dockerfile(tmp_path)
    res, rm = _run(cli_runner, ["--dockerfile", str(df), "--", "x"])
    kw = rm.create_reservation.call_args.kwargs
    payload = kw["dockerfile"]
    assert isinstance(payload, str) and payload
    raw = base64.b64decode(payload)
    # valid gzip magic
    assert raw[:2] == b"\x1f\x8b"
    assert "Building tar.gz context" in res.output
    assert "Dockerfile context" in res.output


def test_dockerfile_too_large_aborts(cli_runner, tmp_path):
    # > 512KB Dockerfile -> abort before building the tar.
    df = _write_dockerfile(tmp_path, content=b"x" * (512 * 1024 + 1))
    res, rm = _run(cli_runner, ["--dockerfile", str(df), "--", "x"])
    assert res.exit_code == 2
    assert "Dockerfile too large" in res.output
    rm.create_reservation.assert_not_called()


def test_dockerfile_nonexistent_path_rejected_by_click(cli_runner, tmp_path):
    # click.Path(exists=True) rejects a missing file -> usage error, exit 2.
    missing = tmp_path / "nope.Dockerfile"
    res, rm = _run(cli_runner, ["--dockerfile", str(missing), "--", "x"])
    assert res.exit_code == 2
    rm.create_reservation.assert_not_called()


def test_dockerfile_alt_name_added_as_Dockerfile_in_tar(cli_runner, tmp_path):
    # A Dockerfile not literally named 'Dockerfile' still gets added under the
    # 'Dockerfile' arcname in the build context.
    df = _write_dockerfile(tmp_path, name="my.Dockerfile", content=b"FROM scratch\n")
    res, rm = _run(cli_runner, ["--dockerfile", str(df), "--", "x"])
    payload = rm.create_reservation.call_args.kwargs["dockerfile"]
    raw = base64.b64decode(payload)
    import io
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
        names = tar.getnames()
    assert "Dockerfile" in names


# ---------------------------------------------------------------------------
# create_reservation kwargs completeness on the success-ish call
# ---------------------------------------------------------------------------
def test_create_reservation_receives_all_expected_kwargs(cli_runner):
    res, rm = _run(
        cli_runner,
        ["--gpu-type", "a100", "--gpus", "2", "--hours", "1", "--", "nvidia-smi"])
    kw = rm.create_reservation.call_args.kwargs
    for key in (
        "user_id", "gpu_count", "gpu_type", "duration_hours", "name",
        "github_user", "no_persistent_disk", "disk_name", "spot",
        "dockerfile", "dockerimage", "preserve_entrypoint", "source_command",
        "ref",
    ):
        assert key in kw, f"missing kwarg {key}"
    assert kw["gpu_count"] == 2
    assert kw["gpu_type"] == "a100"
    assert kw["source_command"] == "submit"


def test_multinode_reservation_kwargs(cli_runner):
    res, rm = _run(
        cli_runner,
        ["--gpu-type", "b200", "--gpus", "16", "--spot", "--ref", "main", "--", "x"],
        multinode_result=None)
    kw = rm.create_multinode_reservation.call_args.kwargs
    assert kw["gpu_count"] == 16
    assert kw["gpu_type"] == "b200"
    assert kw["spot"] is True
    assert kw["ref"] == "main"
    assert kw["source_command"] == "submit"
