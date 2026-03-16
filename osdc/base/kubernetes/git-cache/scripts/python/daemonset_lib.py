"""Extracted testable functions from the daemonset git cache agent.

This module mirrors the pure/testable functions from the daemonset.py script
embedded in daemonset-configmap.yaml. The ConfigMap script is the runtime
copy; this file exists solely to enable unit testing.
"""

import socket
import threading
import time
from pathlib import Path
from typing import ClassVar


class MetricsServer:
    """Thread-safe Prometheus metrics with formatting.

    Simpler variant than central_lib.MetricsServer — no labels, no histograms.
    Copied from daemonset.py (ConfigMap). The HTTP server (start()) is NOT
    included here — only the metric storage and formatting logic.
    """

    # Metric definitions: (name, type, help)
    _METRIC_DEFS: ClassVar[list[tuple[str, str, str]]] = [
        ("git_cache_node_sync_duration_seconds", "gauge", "Duration of last rsync from central"),
        ("git_cache_node_sync_success_total", "counter", "Successful sync count"),
        ("git_cache_node_sync_errors_total", "counter", "Failed sync count"),
        ("git_cache_node_last_sync_timestamp", "gauge", "Unix timestamp of last successful sync"),
        ("git_cache_node_cache_age_seconds", "gauge", "Seconds since last successful sync"),
        ("git_cache_node_active_slot", "gauge", "Current active slot (0=a, 1=b)"),
        ("git_cache_node_initial_sync_duration_seconds", "gauge", "Time from pod start to first successful sync"),
        ("git_cache_node_taint_removed", "gauge", "1 if startup taint was successfully removed, 0 otherwise"),
        ("git_cache_node_central_wait_seconds", "gauge", "Time spent waiting for central rsyncd on startup"),
        ("git_cache_node_cache_size_bytes", "gauge", "Total size of the local git cache on disk"),
    ]

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values: dict[str, float] = {}
        self._types: dict[str, str] = {}
        self._helps: dict[str, str] = {}
        for name, mtype, mhelp in self._METRIC_DEFS:
            self._types[name] = mtype
            self._helps[name] = mhelp
            self._values[name] = 0.0

    def set(self, name: str, value: float) -> None:
        with self._lock:
            self._values[name] = value

    def inc(self, name: str, delta: float = 1.0) -> None:
        with self._lock:
            self._values[name] = self._values.get(name, 0.0) + delta

    def format(self, now: float | None = None) -> str:
        """Format all metrics in Prometheus text exposition format.

        Args:
            now: Current time as Unix timestamp. Used to compute
                cache_age_seconds dynamically. Defaults to time.time().
        """
        if now is None:
            now = time.time()
        lines: list[str] = []
        with self._lock:
            snapshot = dict(self._values)
        for name, mtype, mhelp in self._METRIC_DEFS:
            value = snapshot.get(name, 0.0)
            # cache_age_seconds is computed dynamically
            if name == "git_cache_node_cache_age_seconds":
                last_ts = snapshot.get("git_cache_node_last_sync_timestamp", 0.0)
                value = now - last_ts if last_ts > 0 else 0.0
            lines.append(f"# HELP {name} {mhelp}")
            lines.append(f"# TYPE {name} {mtype}")
            lines.append(f"{name} {value}")
        lines.append("")  # trailing newline
        return "\n".join(lines)


def active_slot(mnt: Path) -> Path | None:
    """Return the currently active slot via the symlink, or None.

    Args:
        mnt: Mount point containing git-cache, git-cache-a, git-cache-b.
    """
    cache_link = mnt / "git-cache"
    slot_a = mnt / "git-cache-a"
    slot_b = mnt / "git-cache-b"
    if cache_link.is_symlink():
        target = cache_link.resolve()
        if target == slot_a.resolve() or target == slot_b.resolve():
            return target
    return None


def staging_slot(mnt: Path) -> Path:
    """Return the slot NOT currently active.

    Args:
        mnt: Mount point containing git-cache, git-cache-a, git-cache-b.
    """
    slot_a = mnt / "git-cache-a"
    slot_b = mnt / "git-cache-b"
    current = active_slot(mnt)
    if current and current == slot_a.resolve():
        return slot_b
    return slot_a


def set_current(mnt: Path, slot: Path) -> None:
    """Atomically swap the git-cache symlink to point to slot.

    Args:
        mnt: Mount point containing the git-cache symlink.
        slot: The slot directory to point to.
    """
    cache_link = mnt / "git-cache"
    tmp_link = mnt / "git-cache.tmp"
    tmp_link.unlink(missing_ok=True)
    tmp_link.symlink_to(slot)
    tmp_link.rename(cache_link)


def _dir_size_bytes(path: Path) -> int:
    """Return total size in bytes of all files under path."""
    total = 0
    try:
        for f in path.rglob("*"):
            if f.is_file():
                total += f.stat().st_size
    except OSError:
        pass
    return total


def wait_for_central(host: str, port: int, timeout: int = 600) -> float:
    """Wait for the central pod's rsyncd to accept TCP connections.

    Args:
        host: Hostname of the central pod.
        port: Port number of the rsyncd service.
        timeout: Maximum seconds to wait.

    Returns:
        Seconds spent waiting.
    """
    t0 = time.monotonic()
    deadline = t0 + timeout
    while time.monotonic() < deadline:
        try:
            sock = socket.create_connection((host, port), timeout=5)
            sock.close()
            return time.monotonic() - t0
        except (TimeoutError, ConnectionRefusedError, OSError):
            time.sleep(5)
    return time.monotonic() - t0


def migrate_old_cache(mnt: Path) -> bool:
    """Migrate old single-directory cache to slot-a.

    If mnt/git-cache is a plain directory (old layout), moves its contents
    into git-cache-a via shutil.copytree, removes the old directory, and
    creates a symlink.

    This is a simplified version for testing — the runtime version uses
    rsync for the copy and calls set_current() for the symlink swap.

    Args:
        mnt: Mount point containing the git-cache directory.

    Returns:
        True if migration was performed, False if no migration needed.
    """
    import shutil

    cache_link = mnt / "git-cache"
    slot_a = mnt / "git-cache-a"

    if cache_link.is_symlink():
        return False  # Already migrated
    if not cache_link.is_dir():
        return False  # Nothing to migrate

    slot_a.mkdir(parents=True, exist_ok=True)

    # Copy old cache contents into slot-a
    for item in cache_link.iterdir():
        src = item
        dst = slot_a / item.name
        if src.is_dir():
            shutil.copytree(src, dst, dirs_exist_ok=True)
        else:
            shutil.copy2(src, dst)

    # Remove old directory and replace with symlink
    shutil.rmtree(cache_link)
    set_current(mnt, slot_a)
    return True
