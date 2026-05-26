"""SSH command execution and file transfer via proxy."""

from __future__ import annotations

import subprocess
from pathlib import Path

from ..common.errors import GpuDevConnectionError, GpuDevTimeoutError
from ..common.models import ExecResult


class SshTransport:
    """Execute commands and transfer files on a sandbox via SSH."""

    def __init__(self, pod_name: str, fqdn: str | None = None) -> None:
        self.pod_name = pod_name
        self.fqdn = fqdn

    def _ssh_base(self) -> list[str]:
        return [
            "ssh",
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "LogLevel=ERROR",
            self.pod_name,
        ]

    def exec(self, command: str, *, timeout: int | None = None) -> ExecResult:
        """Execute a command on the sandbox.

        Args:
            command: Shell command to run.
            timeout: Max seconds to wait. ``None`` means no limit.

        Returns:
            ExecResult with exit_code, stdout, stderr.

        Raises:
            GpuDevConnectionError: SSH connection failed.
            GpuDevTimeoutError: Command exceeded timeout.
        """
        try:
            result = subprocess.run(
                self._ssh_base() + ["--", command],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            return ExecResult(
                exit_code=result.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
            )
        except subprocess.TimeoutExpired:
            raise GpuDevTimeoutError(f"Command timed out after {timeout}s")
        except FileNotFoundError:
            raise GpuDevConnectionError("ssh binary not found")
        except Exception as e:
            raise GpuDevConnectionError(f"SSH failed: {e}")

    def upload(self, local_path: str, remote_path: str) -> None:
        """Upload a file or directory via rsync over SSH.

        Args:
            local_path: Local source path.
            remote_path: Destination path on the sandbox.

        Raises:
            GpuDevConnectionError: Transfer failed.
        """
        src = str(Path(local_path).resolve())
        try:
            result = subprocess.run(
                [
                    "rsync", "-az", "-e",
                    "ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR",
                    src, f"{self.pod_name}:{remote_path}",
                ],
                capture_output=True, text=True, timeout=300,
            )
            if result.returncode != 0:
                raise GpuDevConnectionError(f"Upload failed: {result.stderr}")
        except subprocess.TimeoutExpired:
            raise GpuDevTimeoutError("Upload timed out")

    def download(self, remote_path: str, local_path: str) -> None:
        """Download a file or directory via rsync over SSH.

        Args:
            remote_path: Source path on the sandbox.
            local_path: Local destination path.

        Raises:
            GpuDevConnectionError: Transfer failed.
        """
        try:
            result = subprocess.run(
                [
                    "rsync", "-az", "-e",
                    "ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR",
                    f"{self.pod_name}:{remote_path}", str(Path(local_path).resolve()),
                ],
                capture_output=True, text=True, timeout=300,
            )
            if result.returncode != 0:
                raise GpuDevConnectionError(f"Download failed: {result.stderr}")
        except subprocess.TimeoutExpired:
            raise GpuDevTimeoutError("Download timed out")
