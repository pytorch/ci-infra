"""Sandbox: a handle to a reserved GPU development environment."""

from __future__ import annotations

import time

from .._backend.protocol import Backend
from .._transport.ssh import SshTransport
from ..common.enums import ReservationStatus
from ..common.errors import GpuDevError, GpuDevTimeoutError
from ..common.models import ExecResult, ReservationInfo


class Sandbox:
    """Handle to a reserved GPU development environment.

    Returned by :meth:`GpuDev.reserve` and :meth:`GpuDev.get`.

    Example::

        sandbox = client.reserve(gpu_type="h100", gpu_count=2)
        result = sandbox.exec("nvidia-smi")
        print(result.stdout)
        sandbox.cancel()

    Works as a context manager — automatically cancels on exit::

        with client.reserve(gpu_type="t4") as sb:
            sb.exec("python train.py")
        # reservation cancelled automatically
    """

    def __init__(
        self,
        info: ReservationInfo,
        backend: Backend,
        user_id: str,
    ) -> None:
        self._info = info
        self._backend = backend
        self._user_id = user_id
        self._transport: SshTransport | None = None

    def _get_transport(self) -> SshTransport:
        if self._transport is None:
            if not self._info.pod_name:
                raise GpuDevError("Sandbox not ready — no pod assigned yet")
            self._transport = SshTransport(self._info.pod_name, self._info.fqdn)
        return self._transport

    # ── Properties ──

    @property
    def id(self) -> str:
        """Reservation ID."""
        return self._info.id

    @property
    def status(self) -> ReservationStatus:
        """Current status."""
        return self._info.status

    @property
    def gpu_type(self) -> str:
        """GPU type (e.g. ``"h100"``)."""
        return self._info.gpu_type

    @property
    def gpu_count(self) -> int:
        """Number of GPUs allocated."""
        return self._info.gpu_count

    @property
    def name(self) -> str | None:
        """Human-readable name."""
        return self._info.name

    @property
    def pod_name(self) -> str | None:
        """Pod hostname for SSH."""
        return self._info.pod_name

    @property
    def fqdn(self) -> str | None:
        """Fully-qualified domain name."""
        return self._info.fqdn

    @property
    def ssh_command(self) -> str | None:
        """Copy-pasteable SSH command."""
        return self._info.ssh_command

    @property
    def expires_at(self) -> str | None:
        """Expiration timestamp (ISO 8601)."""
        return self._info.expires_at

    @property
    def jupyter_url(self) -> str | None:
        """Jupyter Lab URL if enabled."""
        return self._info.jupyter_url

    @property
    def disk_name(self) -> str | None:
        """Persistent disk name (if attached)."""
        return self._info.disk_name

    @property
    def detailed_status(self) -> str | None:
        """Detailed status message from the server."""
        return self._info.detailed_status

    @property
    def instance_type(self) -> str | None:
        """EC2 instance type (e.g. ``"p5.48xlarge"``)."""
        return self._info.instance_type

    @property
    def created_at(self) -> str | None:
        """Creation timestamp (ISO 8601)."""
        return self._info.created_at

    @property
    def user_id(self) -> str | None:
        """Owner's user ID."""
        return self._info.user_id

    @property
    def node_ip(self) -> str | None:
        """Node public IP."""
        return self._info.node_ip

    @property
    def is_active(self) -> bool:
        """Whether the sandbox is running and ready for commands."""
        return self._info.status == ReservationStatus.ACTIVE

    @property
    def info(self) -> ReservationInfo:
        """Full reservation details."""
        return self._info

    # ── Command Execution ──

    def exec(self, command: str, *, timeout: int | None = None) -> ExecResult:
        """Execute a shell command in the sandbox.

        Args:
            command: Shell command to run.
            timeout: Max seconds to wait. ``None`` means no limit.

        Returns:
            :class:`ExecResult` with ``exit_code``, ``stdout``, ``stderr``.

        Raises:
            GpuDevConnectionError: SSH connection failed.
            GpuDevTimeoutError: Command exceeded timeout.
        """
        return self._get_transport().exec(command, timeout=timeout)

    # ── File Transfer ──

    def upload(self, local_path: str, remote_path: str) -> None:
        """Upload file or directory to the sandbox via rsync.

        Args:
            local_path: Local source path.
            remote_path: Destination path on the sandbox.
        """
        self._get_transport().upload(local_path, remote_path)

    def download(self, remote_path: str, local_path: str) -> None:
        """Download file or directory from the sandbox via rsync.

        Args:
            remote_path: Source path on the sandbox.
            local_path: Local destination path.
        """
        self._get_transport().download(remote_path, local_path)

    # ── Lifecycle ──

    def cancel(self) -> None:
        """Cancel this reservation."""
        self._backend.cancel_reservation(self._info.id, self._user_id)
        self._info.status = ReservationStatus.CANCELLED

    def extend(self, hours: float) -> None:
        """Extend the reservation duration.

        Args:
            hours: Additional hours (max total is typically 48h).
        """
        self._backend.extend_reservation(self._info.id, self._user_id, hours)

    def refresh(self) -> None:
        """Refresh reservation data from the server."""
        updated = self._backend.poll_reservation_status(self._info.id)
        if updated:
            self._info = updated
            self._transport = None

    def wait_until_ready(
        self,
        timeout_minutes: int = 30,
        on_progress: "Callable[[str, float], None] | None" = None,
    ) -> None:
        """Block until the reservation becomes active.

        Args:
            timeout_minutes: Maximum wait time.
            on_progress: Optional callback ``(message, elapsed_seconds) -> None``.
                Called whenever the status changes. Use ``print`` for simple logging::

                    sandbox.wait_until_ready(on_progress=lambda msg, t: print(f"[{t:.0f}s] {msg}"))

        Raises:
            GpuDevTimeoutError: Reservation did not activate in time.
            GpuDevError: Reservation failed.
        """
        start = time.time()
        deadline = start + timeout_minutes * 60
        delay = 0.5
        last_msg = ""
        while time.time() < deadline:
            self.refresh()
            elapsed = time.time() - start

            if self._info.status == ReservationStatus.ACTIVE:
                if on_progress:
                    on_progress("Ready", elapsed)
                return
            if self._info.status == ReservationStatus.FAILED:
                raise GpuDevError(
                    f"Reservation failed: {self._info.failure_reason or 'unknown'}"
                )
            if self._info.status == ReservationStatus.CANCELLED:
                raise GpuDevError("Reservation was cancelled")

            if on_progress:
                msg = self._info.detailed_status or self._info.status.value
                if msg != last_msg:
                    on_progress(msg, elapsed)
                    last_msg = msg

            time.sleep(delay)
            delay = min(delay + 0.5, 3.0)
        raise GpuDevTimeoutError(
            f"Reservation did not activate within {timeout_minutes} minutes"
        )

    def add_user(self, github_username: str) -> None:
        """Grant SSH access to another GitHub user.

        Args:
            github_username: GitHub username to add.
        """
        self._backend.add_user(self._info.id, self._user_id, github_username)

    # ── Logs ──

    def logs(self) -> list[dict[str, str]]:
        """Get the status history / processing log for this reservation.

        Returns all status transitions with timestamps — shows exactly what
        happened during reservation setup (disk creation, pod scheduling,
        SSH readiness, errors, etc.).

        Returns:
            List of ``{"timestamp": "...", "message": "..."}`` dicts.

        Example::

            for entry in sandbox.logs():
                print(f"[{entry['timestamp']}] {entry['message']}")
        """
        from .._backend.aws import _get_session, _PREFIX
        session = _get_session()
        region = getattr(self._backend, "_region", "us-east-2")
        ddb = session.resource("dynamodb", region_name=region)
        table = ddb.Table(f"{_PREFIX}-reservations")

        try:
            resp = table.get_item(
                Key={"reservation_id": self._info.id},
                ProjectionExpression="status_history",
            )
            history = resp.get("Item", {}).get("status_history", [])
            return [
                {
                    "timestamp": str(entry.get("timestamp", "")),
                    "message": str(entry.get("message", "")),
                }
                for entry in history
            ]
        except Exception:
            return []

    def pod_logs(self, lines: int = 50) -> str:
        """Fetch container stdout from the running pod via SSH.

        Args:
            lines: Number of recent log lines.

        Returns:
            Log output as a string.
        """
        result = self.exec(
            f"cat /proc/1/fd/1 2>/dev/null | tail -{lines} || "
            f"journalctl -n {lines} 2>/dev/null || "
            f"echo 'No logs available'",
            timeout=10,
        )
        return result.stdout

    # ── Context Manager ──

    def __enter__(self) -> Sandbox:
        return self

    def __exit__(self, *args: object) -> None:
        if self.is_active:
            self.cancel()

    def __repr__(self) -> str:
        return (
            f"Sandbox(id='{self.id[:8]}', status='{self.status.value}', "
            f"gpu={self.gpu_count}x{self.gpu_type})"
        )
