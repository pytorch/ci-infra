"""Data models for the GPU Dev SDK."""

from __future__ import annotations

from pydantic import BaseModel

from .enums import GpuType, ReservationStatus


class ReservationParams(BaseModel):
    """Parameters for creating a new reservation."""
    gpu_type: GpuType | str = GpuType.A100
    gpu_count: int = 1
    duration_hours: float = 8.0
    name: str | None = None
    jupyter: bool = False
    disk_name: str | None = None
    docker_image: str | None = None
    dockerfile_path: str | None = None
    ref: str | None = None
    preserve_entrypoint: bool = False
    recreate_env: bool = False
    spot: bool = False


class ReservationInfo(BaseModel):
    """Reservation details returned by the backend."""
    id: str
    status: ReservationStatus
    gpu_type: str
    gpu_count: int = 1
    name: str | None = None
    created_at: str | None = None
    launched_at: str | None = None
    expires_at: str | None = None
    ssh_command: str | None = None
    pod_name: str | None = None
    fqdn: str | None = None
    node_ip: str | None = None
    instance_type: str | None = None
    failure_reason: str | None = None
    detailed_status: str | None = None
    jupyter_url: str | None = None
    jupyter_enabled: bool = False
    disk_name: str | None = None
    is_multinode: bool = False
    user_id: str | None = None


class GpuAvailability(BaseModel):
    """Availability information for a GPU type."""
    gpu_type: str
    available: int
    total: int
    max_reservable: int = 0
    queue_length: int = 0
    estimated_wait_minutes: int = 0


class DiskInfo(BaseModel):
    """Persistent disk information."""
    name: str
    size_gb: int = 0
    snapshot_count: int = 0
    in_use: bool = False
    reservation_id: str | None = None
    is_deleted: bool = False


class ExecResult(BaseModel):
    """Result of a command executed in a sandbox."""
    exit_code: int
    stdout: str = ""
    stderr: str = ""
