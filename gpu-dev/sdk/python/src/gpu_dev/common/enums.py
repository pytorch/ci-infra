"""GPU types and reservation states."""

from enum import Enum


class GpuType(str, Enum):
    """Available GPU types for reservation."""
    H100 = "h100"
    H200 = "h200"
    B200 = "b200"
    B300 = "b300"
    A100 = "a100"
    T4 = "t4"
    L4 = "l4"
    A10G = "a10g"
    RTX_PRO_6000 = "rtxpro6000"
    H100_MIG_1G = "h100-mig-1g"
    H100_MIG_2G = "h100-mig-2g"
    H100_MIG_3G = "h100-mig-3g"
    B200_MIG_1G = "b200-mig-1g"
    B200_MIG_2G = "b200-mig-2g"
    B200_MIG_3G = "b200-mig-3g"
    CPU_ARM = "cpu-arm"
    CPU_X86 = "cpu-x86"


class ReservationStatus(str, Enum):
    """Reservation lifecycle states."""
    PENDING = "pending"
    QUEUED = "queued"
    PREPARING = "preparing"
    ACTIVE = "active"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    FAILED = "failed"


GPU_MAX_COUNT: dict[str, int] = {
    "h100": 8, "h200": 8, "b200": 8, "b300": 8, "a100": 8,
    "t4": 4, "l4": 4, "a10g": 4, "rtxpro6000": 4,
    "h100-mig-1g": 1, "h100-mig-2g": 1, "h100-mig-3g": 1,
    "b200-mig-1g": 1, "b200-mig-2g": 1, "b200-mig-3g": 1,
    "cpu-arm": 0, "cpu-x86": 0,
}
