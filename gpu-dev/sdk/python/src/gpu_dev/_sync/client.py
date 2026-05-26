"""Synchronous GPU Dev client."""

from __future__ import annotations

from .._backend.aws import AwsBackend
from .._backend.protocol import Backend
from ..common.config import GpuDevConfig
from ..common.enums import GPU_MAX_COUNT, ReservationStatus
from ..common.errors import GpuDevValidationError
from ..common.models import DiskInfo, GpuAvailability, ReservationInfo
from .sandbox import Sandbox


class GpuDev:
    """Main client for GPU development server reservations.

    Example::

        from gpu_dev import GpuDev

        client = GpuDev()

        # Reserve 2 H100 GPUs for 4 hours
        sandbox = client.reserve(gpu_type="h100", gpu_count=2, hours=4)
        print(sandbox.ssh_command)

        # Execute a command
        result = sandbox.exec("nvidia-smi")
        print(result.stdout)

        # Upload code and run it
        sandbox.upload("./train.py", "/home/dev/train.py")
        sandbox.exec("python /home/dev/train.py")

        # Clean up
        sandbox.cancel()

    With context manager (auto-cancel)::

        with client.reserve(gpu_type="t4") as sb:
            sb.exec("python train.py")

    Custom config::

        from gpu_dev import GpuDev, GpuDevConfig
        config = GpuDevConfig(github_user="octocat", environment="prod")
        client = GpuDev(config)
    """

    def __init__(self, config: GpuDevConfig | None = None) -> None:
        self._config = config or GpuDevConfig.from_file()
        self._backend: Backend = AwsBackend(self._config)
        self._other_backend: Backend | None = None
        self._user_info: dict[str, str] | None = None

    def _auth(self) -> dict[str, str]:
        if self._user_info is None:
            self._user_info = self._backend.authenticate()
        return self._user_info

    def reserve(
        self,
        gpu_type: str = "a100",
        gpu_count: int = 1,
        hours: float = 8.0,
        *,
        name: str | None = None,
        jupyter: bool = False,
        disk_name: str | None = None,
        docker_image: str | None = None,
        spot: bool = False,
        wait: bool = True,
        timeout_minutes: int | None = None,
    ) -> Sandbox:
        """Reserve GPU resources and return a Sandbox handle.

        Args:
            gpu_type: GPU type (``"h100"``, ``"b200"``, ``"a100"``, ``"t4"``, ...).
            gpu_count: Number of GPUs (1, 2, 4, or 8 depending on type).
            hours: Duration in hours (max 48 with extensions).
            name: Optional human-readable name for the reservation.
            jupyter: Enable Jupyter Lab access.
            disk_name: Persistent disk to attach.
            docker_image: Custom Docker image instead of default.
            spot: Use spot instances (cheaper, may be preempted).
            wait: Block until reservation is active (default ``True``).
            timeout_minutes: Max wait time. Defaults to config value.

        Returns:
            :class:`Sandbox` handle to the reserved environment.

        Raises:
            GpuDevValidationError: Invalid GPU type or count.
            GpuDevTimeoutError: Reservation did not activate in time.
            GpuDevAuthError: Authentication failed.

        Example::

            sandbox = client.reserve(gpu_type="h100", gpu_count=2, hours=4)
            print(f"SSH: {sandbox.ssh_command}")
        """
        gpu_type_lower = gpu_type.lower()
        max_gpus = GPU_MAX_COUNT.get(gpu_type_lower)
        if max_gpus is None:
            raise GpuDevValidationError(
                f"Unknown GPU type: {gpu_type}. "
                f"Available: {', '.join(sorted(GPU_MAX_COUNT.keys()))}"
            )
        if max_gpus > 0 and gpu_count > max_gpus:
            raise GpuDevValidationError(
                f"{gpu_type} supports max {max_gpus} GPUs, got {gpu_count}"
            )

        user_info = self._auth()
        reservation_id = self._backend.create_reservation({
            "user_id": user_info["user_id"],
            "github_user": user_info["github_user"],
            "gpu_type": gpu_type_lower,
            "gpu_count": gpu_count,
            "duration_hours": hours,
            "name": name,
            "jupyter": jupyter,
            "disk_name": disk_name,
            "docker_image": docker_image,
            "spot": spot,
        })

        info = ReservationInfo(
            id=reservation_id,
            status=ReservationStatus.PENDING,
            gpu_type=gpu_type_lower,
            gpu_count=gpu_count,
            name=name,
            user_id=user_info["user_id"],
        )
        sandbox = Sandbox(info, self._backend, user_info["user_id"])

        if wait:
            tm = timeout_minutes or self._config.default_timeout_minutes
            sandbox.wait_until_ready(tm)

        return sandbox

    def get(self, reservation_id: str) -> Sandbox:
        """Get a Sandbox handle for an existing reservation.

        Args:
            reservation_id: Full UUID or 8+ char prefix.

        Returns:
            :class:`Sandbox` handle.

        Raises:
            GpuDevNotFoundError: Reservation not found.

        Example::

            sandbox = client.get("abc12345")
            result = sandbox.exec("nvidia-smi")
        """
        from ..common.errors import GpuDevNotFoundError

        user_info = self._auth()
        info = self._backend.get_reservation(reservation_id, user_info["user_id"])
        if not info:
            raise GpuDevNotFoundError(f"Reservation {reservation_id} not found")
        return Sandbox(info, self._backend, user_info["user_id"])

    def list(
        self,
        *,
        status: list[str] | None = None,
    ) -> list[Sandbox]:
        """List reservations as Sandbox objects.

        Args:
            status: Filter by status(es). Default shows active/pending/queued/preparing.

        Returns:
            List of :class:`Sandbox` handles.

        Example::

            for sb in client.list():
                print(f"{sb.id[:8]} {sb.gpu_type}x{sb.gpu_count} {sb.status}")
        """
        user_info = self._auth()
        statuses = status or ["active", "pending", "queued", "preparing"]

        # Initialize cross-region backend lazily
        if self._other_backend is None:
            try:
                from .._backend.aws import AwsBackend, _ENVIRONMENTS
                other_envs = {"prod": "prod-east1", "prod-east1": "prod"}
                other_env = other_envs.get(self._config.environment)
                if other_env:
                    self._other_backend = AwsBackend(GpuDevConfig(
                        github_user=self._config.github_user,
                        environment=other_env,
                        region=_ENVIRONMENTS.get(other_env, {}).get("region"),
                    ))
            except Exception:
                pass

        # Query both regions in parallel
        from concurrent.futures import ThreadPoolExecutor
        uid = user_info["user_id"]
        with ThreadPoolExecutor(max_workers=2) as ex:
            f1 = ex.submit(self._backend.list_reservations, uid, statuses)
            f2 = ex.submit(self._other_backend.list_reservations, uid, statuses) if self._other_backend else None
            sandboxes = [Sandbox(info, self._backend, uid) for info in f1.result()]
            if f2:
                sandboxes.extend(Sandbox(info, self._other_backend, uid) for info in f2.result())

        return sandboxes

    def availability(self) -> dict[str, GpuAvailability]:
        """Get GPU availability by type.

        Returns:
            Dict mapping GPU type string to :class:`GpuAvailability`.

        Example::

            for gpu, info in client.availability().items():
                print(f"{gpu}: {info.available}/{info.total} available")
        """
        return self._backend.get_availability()

    def disks(self) -> list[DiskInfo]:
        """List persistent disks for the current user.

        Returns:
            List of :class:`DiskInfo`.
        """
        user_info = self._auth()
        return self._backend.list_disks(user_info["user_id"])
