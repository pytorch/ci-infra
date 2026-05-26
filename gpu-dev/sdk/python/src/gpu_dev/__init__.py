"""GPU Dev SDK — Python SDK for GPU development server reservations.

Quick start::

    from gpu_dev import GpuDev

    client = GpuDev()
    sandbox = client.reserve(gpu_type="h100", gpu_count=2, hours=4)
    result = sandbox.exec("nvidia-smi")
    print(result.stdout)
    sandbox.cancel()

Context manager (auto-cancel)::

    with client.reserve(gpu_type="t4") as sb:
        sb.exec("python train.py")
"""

from .common.config import GpuDevConfig
from .common.enums import GpuType, ReservationStatus
from .common.errors import (
    GpuDevAuthError,
    GpuDevCapacityError,
    GpuDevConnectionError,
    GpuDevError,
    GpuDevNotFoundError,
    GpuDevTimeoutError,
    GpuDevValidationError,
)
from .common.models import (
    DiskInfo,
    ExecResult,
    GpuAvailability,
    ReservationInfo,
    ReservationParams,
)
from ._sync.client import GpuDev
from ._sync.sandbox import Sandbox

__all__ = [
    "GpuDev",
    "Sandbox",
    "GpuDevConfig",
    "GpuType",
    "ReservationStatus",
    "GpuDevError",
    "GpuDevAuthError",
    "GpuDevNotFoundError",
    "GpuDevTimeoutError",
    "GpuDevValidationError",
    "GpuDevConnectionError",
    "GpuDevCapacityError",
    "ReservationInfo",
    "ReservationParams",
    "GpuAvailability",
    "DiskInfo",
    "ExecResult",
]

__version__ = "0.6.4"
