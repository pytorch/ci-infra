"""Extracted testable functions from the central git cache manager.

This module mirrors the pure/testable functions from the central.py script
embedded in central-configmap.yaml. The ConfigMap script is the runtime
copy; this file exists solely to enable unit testing.
"""

import threading
from pathlib import Path
from typing import ClassVar


class MetricsServer:
    """Prometheus metrics server — thread-safe counters, gauges, histograms.

    Copied from central.py (ConfigMap). Serves /metrics in Prometheus text
    exposition format. The HTTP server (start()) is NOT included here —
    only the metric storage and formatting logic.
    """

    METRICS_PORT = 9101

    METRIC_DEFS: ClassVar[dict[str, tuple[str, str]]] = {
        "git_cache_central_fetch_duration_seconds": ("gauge", "Duration of last git fetch per repo"),
        "git_cache_central_fetch_success_total": ("counter", "Successful fetch count per repo"),
        "git_cache_central_fetch_errors_total": ("counter", "Failed fetch count per repo"),
        "git_cache_central_last_fetch_timestamp": ("gauge", "Unix timestamp of last successful fetch"),
        "git_cache_central_repo_size_bytes": ("gauge", "Size of each repo cache on disk"),
        "git_cache_central_slot_swaps_total": ("counter", "Number of active-slot rotations"),
        "git_cache_central_active_slot": ("gauge", "Current active slot (0=a, 1=b)"),
        "git_cache_central_cycle_duration_seconds": ("gauge", "Duration of last full update cycle"),
        "git_cache_central_rsyncd_restarts_total": ("counter", "rsyncd daemon restart count"),
        "git_cache_central_repos_total": ("gauge", "Number of repos being cached"),
        "git_cache_central_clone_total": ("counter", "Total clone/fetch operations"),
        "git_cache_central_clone_duration_seconds": ("histogram", "Time to clone/fetch each repo"),
        "git_cache_central_last_success_timestamp": ("gauge", "Unix timestamp of last successful sync per repo"),
    }

    HISTOGRAM_BUCKETS = (1.0, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0, 600.0, float("inf"))

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values: dict[tuple[str, frozenset], float] = {}
        self._histograms: dict[tuple[str, frozenset], dict] = {}

    def set(self, name: str, value: float, labels: dict[str, str] | None = None) -> None:
        """Set a gauge metric value."""
        key = (name, frozenset((labels or {}).items()))
        with self._lock:
            self._values[key] = value

    def inc(self, name: str, labels: dict[str, str] | None = None, amount: float = 1) -> None:
        """Increment a counter metric."""
        key = (name, frozenset((labels or {}).items()))
        with self._lock:
            self._values[key] = self._values.get(key, 0) + amount

    def observe(self, name: str, value: float, labels: dict[str, str] | None = None) -> None:
        """Record an observation in a histogram metric."""
        key = (name, frozenset((labels or {}).items()))
        with self._lock:
            if key not in self._histograms:
                self._histograms[key] = {
                    "buckets": dict.fromkeys(self.HISTOGRAM_BUCKETS, 0),
                    "sum": 0.0,
                    "count": 0,
                }
            h = self._histograms[key]
            for le in sorted(self.HISTOGRAM_BUCKETS):
                if value <= le:
                    h["buckets"][le] += 1
                    break
            h["sum"] += value
            h["count"] += 1

    def _format_label_str(self, labels_dict: dict[str, str]) -> str:
        """Format a dict of labels into Prometheus label string."""
        if not labels_dict:
            return ""
        return "{" + ",".join(f'{k}="{v}"' for k, v in sorted(labels_dict.items())) + "}"

    def format_metrics(self) -> str:
        """Format all metrics in Prometheus text exposition format."""
        with self._lock:
            snapshot = dict(self._values)
            hist_snapshot = {
                k: {
                    "buckets": dict(v["buckets"]),
                    "sum": v["sum"],
                    "count": v["count"],
                }
                for k, v in self._histograms.items()
            }

        by_name: dict[str, list[tuple[frozenset, float]]] = {}
        for (name, label_set), value in snapshot.items():
            by_name.setdefault(name, []).append((label_set, value))

        hist_by_name: dict[str, list[tuple[frozenset, dict]]] = {}
        for (name, label_set), hdata in hist_snapshot.items():
            hist_by_name.setdefault(name, []).append((label_set, hdata))

        all_names = sorted(set(list(by_name.keys()) + list(hist_by_name.keys())))

        lines: list[str] = []
        for name in all_names:
            mtype, help_text = self.METRIC_DEFS.get(name, ("untyped", ""))
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} {mtype}")

            if name in hist_by_name:
                for label_set, hdata in sorted(hist_by_name[name]):
                    base_labels = dict(label_set)
                    cumulative = 0
                    for le in sorted(b for b in hdata["buckets"] if b != float("inf")):
                        cumulative += hdata["buckets"][le]
                        le_labels = {**base_labels, "le": str(le)}
                        lines.append(f"{name}_bucket{self._format_label_str(le_labels)} {cumulative}")
                    cumulative += hdata["buckets"].get(float("inf"), 0)
                    inf_labels = {**base_labels, "le": "+Inf"}
                    lines.append(f"{name}_bucket{self._format_label_str(inf_labels)} {cumulative}")
                    base_lstr = self._format_label_str(base_labels)
                    lines.append(f"{name}_sum{base_lstr} {hdata['sum']}")
                    lines.append(f"{name}_count{base_lstr} {hdata['count']}")
            else:
                for label_set, value in sorted(by_name.get(name, [])):
                    labels_dict = dict(label_set)
                    if labels_dict:
                        label_str = ",".join(f'{k}="{v}"' for k, v in sorted(labels_dict.items()))
                        lines.append(f"{name}{{{label_str}}} {value}")
                    else:
                        lines.append(f"{name} {value}")
        lines.append("")
        return "\n".join(lines)


def active_slot(cache_dir: Path) -> Path | None:
    """Return the currently active slot, or None if no valid link.

    Args:
        cache_dir: The data directory containing cache-a, cache-b, and the
                   ``current`` symlink.
    """
    slot_a = cache_dir / "cache-a"
    slot_b = cache_dir / "cache-b"
    current_link = cache_dir / "current"
    if current_link.is_symlink():
        target = current_link.resolve()
        if target == slot_a.resolve() or target == slot_b.resolve():
            return target
    return None


def staging_slot(cache_dir: Path) -> Path:
    """Return the slot NOT currently active (for updates).

    Args:
        cache_dir: The data directory containing cache-a, cache-b, and the
                   ``current`` symlink.
    """
    slot_a = cache_dir / "cache-a"
    slot_b = cache_dir / "cache-b"
    current = active_slot(cache_dir)
    if current and current == slot_a.resolve():
        return slot_b
    return slot_a


def slot_is_valid(slot: Path, repos_full: list[str], repos_bare: list[str]) -> bool:
    """Check if a slot contains a populated cache.

    Args:
        slot: Path to the slot directory (e.g. cache-a).
        repos_full: List of ``org/name`` repos cloned with submodules.
        repos_bare: List of ``org/name`` repos cloned bare.
    """
    for repo in repos_full:
        org, name = repo.split("/")
        if (slot / org / name / ".git").is_dir():
            return True
    for repo in repos_bare:
        org, name = repo.split("/")
        if (slot / org / f"{name}.git" / "objects").is_dir():
            return True
    return False


def set_current(cache_dir: Path, slot: Path) -> None:
    """Atomically swap the current symlink to point to slot.

    Args:
        cache_dir: The data directory containing the ``current`` symlink.
        slot: The slot directory to point to.
    """
    current_link = cache_dir / "current"
    tmp_link = cache_dir / "current.tmp"
    tmp_link.unlink(missing_ok=True)
    tmp_link.symlink_to(slot)
    tmp_link.rename(current_link)


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
