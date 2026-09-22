"""Tests for SDK models and validation."""

from gpu_dev import GpuDev, GpuDevConfig, GpuType, ReservationStatus, GpuDevValidationError
from gpu_dev.common.enums import GPU_MAX_COUNT
from gpu_dev.common.models import ReservationInfo, ExecResult, GpuAvailability
import pytest


def test_gpu_type_values():
    assert GpuType.H100.value == "h100"
    assert GpuType.B200.value == "b200"
    assert GpuType.T4.value == "t4"


def test_reservation_status():
    assert ReservationStatus.ACTIVE.value == "active"
    assert ReservationStatus.PENDING.value == "pending"


def test_gpu_max_count():
    assert GPU_MAX_COUNT["h100"] == 8
    assert GPU_MAX_COUNT["t4"] == 4
    assert GPU_MAX_COUNT["h100-mig-1g"] == 1


def test_reservation_info():
    info = ReservationInfo(
        id="abc-123",
        status=ReservationStatus.ACTIVE,
        gpu_type="h100",
        gpu_count=2,
        pod_name="gpu-dev-abc12345",
    )
    assert info.id == "abc-123"
    assert info.is_multinode is False


def test_exec_result():
    result = ExecResult(exit_code=0, stdout="hello\n", stderr="")
    assert result.exit_code == 0
    assert "hello" in result.stdout


def test_config_defaults():
    config = GpuDevConfig()
    assert config.environment == "prod"
    assert config.default_timeout_minutes == 30


def test_validation_bad_gpu_type():
    config = GpuDevConfig(github_user="test")
    client = GpuDev.__new__(GpuDev)
    client._config = config
    client._backend = None
    client._user_info = {"user_id": "test", "github_user": "test"}

    with pytest.raises(GpuDevValidationError, match="Unknown GPU type"):
        client.reserve(gpu_type="nvidia-rtx-9090")


def test_validation_gpu_count_exceeded():
    config = GpuDevConfig(github_user="test")
    client = GpuDev.__new__(GpuDev)
    client._config = config
    client._backend = None
    client._user_info = {"user_id": "test", "github_user": "test"}

    with pytest.raises(GpuDevValidationError, match="max 4"):
        client.reserve(gpu_type="t4", gpu_count=8)
