"""Sliding-window peak tracker for per-group bin-pack minimums."""

import time

from models import PEAK_WINDOW_SECONDS


def prune_stale_peak_history(peak_history: dict[str, list[tuple[float, int]]]) -> None:
    now = time.monotonic()
    cutoff = now - PEAK_WINDOW_SECONDS
    for key in list(peak_history.keys()):
        peak_history[key] = [(t, v) for t, v in peak_history[key] if t >= cutoff]
        if not peak_history[key]:
            peak_history.pop(key)


def update_peak_history(
    peak_history: dict[str, list[tuple[float, int]]],
    group_name: str,
    current_min: int,
    interval: int,
) -> int:
    """Append current bin-pack result, prune by age + length cap, return windowed peak.

    Returns the max bin_pack_min_nodes observed inside PEAK_WINDOW_SECONDS for this group.
    """
    now = time.monotonic()
    history = peak_history.setdefault(group_name, [])
    history.append((now, current_min))
    cutoff = now - PEAK_WINDOW_SECONDS
    max_entries = max(64, PEAK_WINDOW_SECONDS // max(1, interval) + 60)
    history[:] = [(t, v) for t, v in history if t >= cutoff][-max_entries:]
    return max(v for _, v in history)
