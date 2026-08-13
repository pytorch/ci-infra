"""Extracted testable functions from the image cache janitor agent.

This module mirrors the pure/testable functions from the janitor.py script
embedded in configmap.yaml. The ConfigMap script is the runtime copy; this
file exists solely to enable unit testing.
"""

import json
import threading
from dataclasses import dataclass, field
from typing import ClassVar

# ---------------------------------------------------------------------------
# Image info
# ---------------------------------------------------------------------------


@dataclass
class ImageInfo:
    """Parsed container image metadata from crictl."""

    id: str
    repo_tags: list[str] = field(default_factory=list)
    repo_digests: list[str] = field(default_factory=list)
    size: int = 0
    pinned: bool = False


def parse_crictl_images(json_str: str) -> list[ImageInfo]:
    """Parse ``crictl images -o json`` output into ImageInfo objects."""
    data = json.loads(json_str)
    images_raw = data.get("images", [])
    result: list[ImageInfo] = []
    for img in images_raw:
        size_val = img.get("size", 0)
        if isinstance(size_val, str):
            size_val = int(size_val)
        result.append(
            ImageInfo(
                id=img.get("id", ""),
                repo_tags=img.get("repoTags") or [],
                repo_digests=img.get("repoDigests") or [],
                size=size_val,
                pinned=bool(img.get("pinned", False)),
            )
        )
    return result


def calculate_total_cache_size(images: list[ImageInfo]) -> int:
    """Sum the size of all images in bytes."""
    return sum(img.size for img in images)


def select_images_to_remove(
    images: list[ImageInfo],
    total_size: int,
    limit_bytes: int,
    target_bytes: int,
) -> list[ImageInfo]:
    """Select unused images to remove to bring cache under target.

    Strategy: filter out pinned images, sort remaining by size
    descending (largest first), accumulate until total drops below
    target. Returns empty list if total is under the limit.
    """
    if total_size <= limit_bytes:
        return []

    removable = [img for img in images if not img.pinned]
    removable.sort(key=lambda img: img.size, reverse=True)

    to_remove: list[ImageInfo] = []
    projected = total_size
    for img in removable:
        if projected <= target_bytes:
            break
        to_remove.append(img)
        projected -= img.size

    return to_remove


# ---------------------------------------------------------------------------
# Orphaned BuildKit cache directories
# ---------------------------------------------------------------------------


@dataclass
class CacheDirInfo:
    """A directory under BUILDKIT_CACHE_DIR, named after the pod that owns it."""

    name: str
    mtime: float
    size: int = 0


def parse_crictl_pod_names(json_str: str) -> set[str]:
    """Extract pod names from ``crictl pods -o json`` output."""
    data = json.loads(json_str)
    names = set()
    for pod in data.get("items", []):
        name = (pod.get("metadata") or {}).get("name")
        if name:
            names.add(name)
    return names


def select_orphan_cache_dirs(
    dirs: list[CacheDirInfo],
    live_pod_names: set[str],
    now: float,
    grace_seconds: int,
) -> list[CacheDirInfo]:
    """Select cache directories whose owning pod is gone.

    A directory is orphaned when no pod of that name is running on the
    node. The grace period covers the window where kubelet has created
    the directory but the sandbox is not yet visible to crictl.

    An empty live_pod_names is treated as "unknown", not "nothing is
    running": the janitor is itself a pod on this node, so crictl always
    reports at least one. Returning nothing keeps a transient containerd
    hiccup from being read as "every directory is orphaned".
    """
    if not live_pod_names:
        return []
    return [d for d in dirs if d.name not in live_pod_names and (now - d.mtime) >= grace_seconds]


# ---------------------------------------------------------------------------
# Prometheus metrics (no HTTP server — only storage and formatting)
# ---------------------------------------------------------------------------


class MetricsServer:
    """Thread-safe Prometheus metrics with formatting.

    Copied from janitor.py (ConfigMap). The HTTP server (start()) is NOT
    included here — only the metric storage and formatting logic.
    """

    _METRIC_DEFS: ClassVar[list[tuple[str, str, str]]] = [
        ("image_cache_janitor_cache_size_bytes", "gauge", "Total size of container image cache in bytes"),
        ("image_cache_janitor_cache_image_count", "gauge", "Number of container images in the cache"),
        ("image_cache_janitor_gc_cycles_total", "counter", "Total number of GC check cycles run"),
        ("image_cache_janitor_gc_evictions_total", "counter", "Total number of images evicted"),
        (
            "image_cache_janitor_gc_eviction_errors_total",
            "counter",
            "Total number of image eviction failures (in-use images)",
        ),
        ("image_cache_janitor_gc_evicted_bytes_total", "counter", "Total bytes freed by eviction"),
        ("image_cache_janitor_last_cycle_timestamp", "gauge", "Unix timestamp of the last GC cycle"),
        ("image_cache_janitor_cache_limit_bytes", "gauge", "Configured cache size limit in bytes"),
        ("image_cache_janitor_cache_target_bytes", "gauge", "Configured cache target size in bytes"),
    ]

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values: dict[str, float] = {}
        for name, _mtype, _mhelp in self._METRIC_DEFS:
            self._values[name] = 0.0

    def set(self, name: str, value: float) -> None:
        with self._lock:
            self._values[name] = value

    def inc(self, name: str, delta: float = 1.0) -> None:
        with self._lock:
            self._values[name] = self._values.get(name, 0.0) + delta

    def format(self) -> str:
        lines: list[str] = []
        with self._lock:
            snapshot = dict(self._values)
        for name, mtype, mhelp in self._METRIC_DEFS:
            value = snapshot.get(name, 0.0)
            lines.append(f"# HELP {name} {mhelp}")
            lines.append(f"# TYPE {name} {mtype}")
            lines.append(f"{name} {value}")
        lines.append("")  # trailing newline
        return "\n".join(lines)
