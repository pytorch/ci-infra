"""Abstract backend interface.

Backends handle all infrastructure communication (database, queues, etc.).
The public SDK classes delegate to a backend, keeping the API cloud-agnostic.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..common.models import DiskInfo, GpuAvailability, ReservationInfo


@runtime_checkable
class Backend(Protocol):

    def authenticate(self) -> dict[str, str]:
        """Return ``{"user_id": ..., "github_user": ...}``."""
        ...

    def create_reservation(self, params: dict) -> str:
        """Submit a reservation request. Returns ``reservation_id``."""
        ...

    def get_reservation(self, reservation_id: str, user_id: str) -> ReservationInfo | None:
        """Look up a reservation by full or prefix ID."""
        ...

    def list_reservations(
        self, user_id: str | None = None, statuses: list[str] | None = None,
    ) -> list[ReservationInfo]:
        """List reservations, optionally filtered."""
        ...

    def cancel_reservation(self, reservation_id: str, user_id: str) -> bool:
        """Cancel a reservation."""
        ...

    def extend_reservation(self, reservation_id: str, user_id: str, hours: float) -> bool:
        """Extend a reservation's duration."""
        ...

    def get_availability(self) -> dict[str, GpuAvailability]:
        """Return GPU availability keyed by type."""
        ...

    def list_disks(self, user_id: str) -> list[DiskInfo]:
        """List persistent disks for a user."""
        ...

    def add_user(self, reservation_id: str, user_id: str, github_username: str) -> bool:
        """Grant SSH access to another user."""
        ...
