"""Extra unit tests for SDK pydantic models.

Source under test: sdk/python/src/gpu_dev/common/models.py

Covers behaviour NOT already exercised by sdk/python/tests/test_models.py:
defaults, required-field validation, type coercion / error paths, the
``GpuType | str`` union ordering quirk, enum coercion, model_dump round-trips,
GpuAvailability / ReservationParams construction, and is_multinode.
"""

import pytest
from pydantic import ValidationError

from gpu_dev.common.models import (
    ReservationParams,
    ReservationInfo,
    GpuAvailability,
    DiskInfo,
    ExecResult,
)
from gpu_dev.common.enums import GpuType, ReservationStatus


# --------------------------------------------------------------------------- #
# ReservationParams
# --------------------------------------------------------------------------- #
def test_reservation_params_all_defaults():
    p = ReservationParams()
    # gpu_type default is the A100 *enum* member (not a bare string)
    assert p.gpu_type is GpuType.A100
    assert p.gpu_count == 1
    assert p.duration_hours == 8.0
    assert isinstance(p.duration_hours, float)
    assert p.name is None
    assert p.jupyter is False
    assert p.disk_name is None
    assert p.docker_image is None
    assert p.dockerfile_path is None
    assert p.ref is None
    assert p.preserve_entrypoint is False
    assert p.recreate_env is False
    assert p.spot is False


def test_reservation_params_enum_gpu_type_preserved():
    p = ReservationParams(gpu_type=GpuType.B200)
    assert p.gpu_type is GpuType.B200
    assert p.gpu_type.value == "b200"


def test_reservation_params_known_string_stays_string():
    # Union is `GpuType | str`: a string that *is* a valid enum value is NOT
    # coerced to the enum because `str` matches first in left-to-right mode.
    p = ReservationParams(gpu_type="h100")
    assert p.gpu_type == "h100"
    assert isinstance(p.gpu_type, str)
    assert not isinstance(p.gpu_type, GpuType)


def test_reservation_params_unknown_string_allowed():
    # The `| str` arm means arbitrary strings validate (no enum restriction).
    p = ReservationParams(gpu_type="totally-made-up")
    assert p.gpu_type == "totally-made-up"


def test_reservation_params_duration_int_coerced_to_float():
    p = ReservationParams(duration_hours=2)
    assert p.duration_hours == 2.0
    assert isinstance(p.duration_hours, float)


def test_reservation_params_fractional_duration():
    p = ReservationParams(duration_hours=0.25)
    assert p.duration_hours == 0.25


def test_reservation_params_gpu_count_string_coerced():
    p = ReservationParams(gpu_count="4")
    assert p.gpu_count == 4
    assert isinstance(p.gpu_count, int)


def test_reservation_params_gpu_count_bad_string_raises():
    with pytest.raises(ValidationError):
        ReservationParams(gpu_count="not-a-number")


def test_reservation_params_full_construction():
    p = ReservationParams(
        gpu_type="t4",
        gpu_count=2,
        duration_hours=1.5,
        name="my-exp",
        jupyter=True,
        disk_name="scratch",
        docker_image="pytorch/pytorch:latest",
        dockerfile_path="/tmp/Dockerfile",
        ref="pr/12345",
        preserve_entrypoint=True,
        recreate_env=True,
        spot=True,
    )
    assert p.name == "my-exp"
    assert p.jupyter is True
    assert p.disk_name == "scratch"
    assert p.docker_image == "pytorch/pytorch:latest"
    assert p.dockerfile_path == "/tmp/Dockerfile"
    assert p.ref == "pr/12345"
    assert p.preserve_entrypoint is True
    assert p.recreate_env is True
    assert p.spot is True


def test_reservation_params_bool_coercion_from_truthy_string():
    # pydantic v2 coerces common bool-ish strings
    p = ReservationParams(jupyter="true", spot="false")
    assert p.jupyter is True
    assert p.spot is False


# --------------------------------------------------------------------------- #
# ReservationInfo
# --------------------------------------------------------------------------- #
def test_reservation_info_minimal_defaults():
    info = ReservationInfo(id="r-1", status="active", gpu_type="h100")
    assert info.id == "r-1"
    assert info.status is ReservationStatus.ACTIVE
    assert info.gpu_count == 1
    assert info.is_multinode is False
    assert info.jupyter_enabled is False
    # every optional string field defaults to None
    for field in (
        "name", "created_at", "launched_at", "expires_at", "ssh_command",
        "pod_name", "fqdn", "node_ip", "instance_type", "failure_reason",
        "detailed_status", "jupyter_url", "disk_name", "user_id",
    ):
        assert getattr(info, field) is None


def test_reservation_info_status_string_coerced_to_enum():
    info = ReservationInfo(id="x", status="queued", gpu_type="h100")
    assert info.status is ReservationStatus.QUEUED
    assert isinstance(info.status, ReservationStatus)


def test_reservation_info_status_enum_passthrough():
    info = ReservationInfo(id="x", status=ReservationStatus.FAILED, gpu_type="b200")
    assert info.status is ReservationStatus.FAILED


def test_reservation_info_bad_status_raises():
    with pytest.raises(ValidationError):
        ReservationInfo(id="x", status="not-a-real-status", gpu_type="h100")


def test_reservation_info_missing_id_raises():
    with pytest.raises(ValidationError) as exc:
        ReservationInfo(status="active", gpu_type="h100")
    assert "id" in str(exc.value)


def test_reservation_info_missing_status_raises():
    with pytest.raises(ValidationError) as exc:
        ReservationInfo(id="x", gpu_type="h100")
    assert "status" in str(exc.value)


def test_reservation_info_missing_gpu_type_raises():
    with pytest.raises(ValidationError) as exc:
        ReservationInfo(id="x", status="active")
    assert "gpu_type" in str(exc.value)


def test_reservation_info_gpu_count_string_coerced():
    info = ReservationInfo(id="x", status="active", gpu_type="h100", gpu_count="3")
    assert info.gpu_count == 3
    assert isinstance(info.gpu_count, int)


def test_reservation_info_gpu_count_bad_string_raises():
    with pytest.raises(ValidationError):
        ReservationInfo(id="x", status="active", gpu_type="h100", gpu_count="abc")


def test_reservation_info_is_multinode_explicit():
    info = ReservationInfo(
        id="multi", status="active", gpu_type="h100", gpu_count=16, is_multinode=True
    )
    assert info.is_multinode is True
    assert info.gpu_count == 16


def test_reservation_info_extra_field_ignored():
    # pydantic default = ignore extra keys; backend may send fields the SDK
    # doesn't model yet.
    info = ReservationInfo(
        id="x", status="active", gpu_type="h100", some_future_field="surprise"
    )
    assert not hasattr(info, "some_future_field")


def test_reservation_info_jupyter_enabled_and_url():
    info = ReservationInfo(
        id="x",
        status="active",
        gpu_type="h100",
        jupyter_enabled=True,
        jupyter_url="http://1.2.3.4:8888/?token=abc",
    )
    assert info.jupyter_enabled is True
    assert info.jupyter_url.endswith("token=abc")


def test_reservation_info_model_dump_roundtrip():
    info = ReservationInfo(
        id="r1",
        status=ReservationStatus.PENDING,
        gpu_type="b200",
        gpu_count=4,
        name="exp",
        is_multinode=True,
        user_id="u-9",
    )
    dumped = info.model_dump()
    # status stays an enum member on dump (no mode="json")
    assert dumped["status"] is ReservationStatus.PENDING
    assert dumped["gpu_count"] == 4
    assert dumped["is_multinode"] is True
    assert dumped["ssh_command"] is None
    rebuilt = ReservationInfo(**dumped)
    assert rebuilt == info


def test_reservation_info_model_dump_json_mode_serializes_enum():
    info = ReservationInfo(id="r1", status=ReservationStatus.EXPIRED, gpu_type="t4")
    dumped = info.model_dump(mode="json")
    assert dumped["status"] == "expired"
    assert isinstance(dumped["status"], str)


# --------------------------------------------------------------------------- #
# GpuAvailability
# --------------------------------------------------------------------------- #
def test_gpu_availability_defaults():
    ga = GpuAvailability(gpu_type="h100", available=2, total=8)
    assert ga.gpu_type == "h100"
    assert ga.available == 2
    assert ga.total == 8
    assert ga.max_reservable == 0
    assert ga.queue_length == 0
    assert ga.estimated_wait_minutes == 0


def test_gpu_availability_full():
    ga = GpuAvailability(
        gpu_type="b200",
        available=0,
        total=16,
        max_reservable=8,
        queue_length=3,
        estimated_wait_minutes=45,
    )
    assert ga.available == 0
    assert ga.max_reservable == 8
    assert ga.queue_length == 3
    assert ga.estimated_wait_minutes == 45


def test_gpu_availability_missing_required_raises():
    with pytest.raises(ValidationError) as exc:
        GpuAvailability(gpu_type="h100")
    msg = str(exc.value)
    assert "available" in msg
    assert "total" in msg


def test_gpu_availability_int_coercion_from_string():
    ga = GpuAvailability(gpu_type="h100", available="5", total="8")
    assert ga.available == 5
    assert ga.total == 8
    assert isinstance(ga.available, int)


def test_gpu_availability_bad_int_raises():
    with pytest.raises(ValidationError):
        GpuAvailability(gpu_type="h100", available="lots", total=8)


# --------------------------------------------------------------------------- #
# DiskInfo
# --------------------------------------------------------------------------- #
def test_disk_info_defaults():
    dk = DiskInfo(name="scratch")
    assert dk.name == "scratch"
    assert dk.size_gb == 0
    assert dk.snapshot_count == 0
    assert dk.in_use is False
    assert dk.reservation_id is None
    assert dk.is_deleted is False


def test_disk_info_full():
    dk = DiskInfo(
        name="d1",
        size_gb=200,
        snapshot_count=3,
        in_use=True,
        reservation_id="r-42",
        is_deleted=True,
    )
    assert dk.size_gb == 200
    assert dk.snapshot_count == 3
    assert dk.in_use is True
    assert dk.reservation_id == "r-42"
    assert dk.is_deleted is True


def test_disk_info_missing_name_raises():
    with pytest.raises(ValidationError) as exc:
        DiskInfo(size_gb=10)
    assert "name" in str(exc.value)


# --------------------------------------------------------------------------- #
# ExecResult
# --------------------------------------------------------------------------- #
def test_exec_result_defaults():
    r = ExecResult(exit_code=0)
    assert r.exit_code == 0
    assert r.stdout == ""
    assert r.stderr == ""


def test_exec_result_missing_exit_code_raises():
    with pytest.raises(ValidationError) as exc:
        ExecResult()
    assert "exit_code" in str(exc.value)


def test_exec_result_nonzero_with_streams():
    r = ExecResult(exit_code=127, stdout="", stderr="command not found")
    assert r.exit_code == 127
    assert r.stderr == "command not found"


def test_exec_result_exit_code_string_coerced():
    r = ExecResult(exit_code="42")
    assert r.exit_code == 42
    assert isinstance(r.exit_code, int)


def test_exec_result_exit_code_bool_coerced_to_int():
    # bool is a subclass of int; pydantic accepts True -> 1
    r = ExecResult(exit_code=True)
    assert r.exit_code == 1
    assert isinstance(r.exit_code, int)


def test_exec_result_bad_exit_code_raises():
    with pytest.raises(ValidationError):
        ExecResult(exit_code="not-an-int")
