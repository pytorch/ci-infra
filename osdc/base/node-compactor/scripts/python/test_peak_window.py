"""Unit tests for the peak_window sliding-window peak tracker."""

import time

from models import PEAK_WINDOW_SECONDS
from peak_window import update_peak_history


class TestUpdatePeakHistory:
    """Tests for update_peak_history (direct, no compute_taints)."""

    def test_update_peak_history_prunes_stale_entries(self):
        """update_peak_history drops entries older than PEAK_WINDOW_SECONDS."""
        now = time.monotonic()
        stale_ts = now - PEAK_WINDOW_SECONDS - 1
        peak_history: dict[str, list[tuple[float, int]]] = {
            "pool-a": [(stale_ts, 99), (now - 5, 2)],
        }

        peak = update_peak_history(peak_history, "pool-a", current_min=3, interval=20)

        timestamps = [t for t, _ in peak_history["pool-a"]]
        assert stale_ts not in timestamps
        assert peak == 3
