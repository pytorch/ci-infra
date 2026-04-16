"""Unit tests for the zombie-cleanup metrics module."""

import logging
from typing import ClassVar
from unittest.mock import patch

from zombie_metrics import push_metrics, registry

# --- Registry contents ---


class TestMetricRegistration:
    """Verify all expected metrics are present in the custom registry."""

    # prometheus_client registers Counter families without the _total suffix;
    # the suffix is added to samples only.  So the family name is
    # "zombie_cleanup_runs", not "zombie_cleanup_runs_total".
    EXPECTED_METRICS: ClassVar[set[str]] = {
        "zombie_cleanup_runs",
        "zombie_cleanup_pods_total",
        "zombie_cleanup_zombies_found",
        "zombie_cleanup_pods_deleted",
        "zombie_cleanup_pods_failed",
        "zombie_cleanup_pods_skipped",
        "zombie_cleanup_duration_seconds",
        "zombie_cleanup_pods_managed_skipped",
        "zombie_cleanup_oldest_zombie_age_hours",
    }

    def _registered_names(self) -> set[str]:
        """Collect all metric family names from the custom registry."""
        return {metric.name for metric in registry.collect()}

    def test_all_metrics_registered(self):
        names = self._registered_names()
        for expected in self.EXPECTED_METRICS:
            assert expected in names, f"Missing metric: {expected}"

    def test_no_unexpected_metrics(self):
        names = self._registered_names()
        unexpected = names - self.EXPECTED_METRICS
        assert not unexpected, f"Unexpected metrics in registry: {unexpected}"

    def test_naming_convention(self):
        """All metric family names start with 'zombie_cleanup_'."""
        for name in self._registered_names():
            assert name.startswith("zombie_cleanup_"), f"Metric {name!r} violates naming convention"

    def test_counter_samples_use_total_suffix(self):
        """Counter samples are emitted with '_total' suffix after increment."""
        from zombie_metrics import runs_total

        # Labeled counters don't emit samples until a label set is initialized
        runs_total.labels(status="test").inc(0)
        sample_names = set()
        for metric in registry.collect():
            for sample in metric.samples:
                sample_names.add(sample.name)
        assert "zombie_cleanup_runs_total" in sample_names


# --- push_metrics ---


class TestPushMetrics:
    @patch("zombie_metrics.push_to_gateway")
    def test_calls_push_to_gateway(self, mock_push):
        push_metrics("http://pushgw:9091")
        mock_push.assert_called_once_with(
            "http://pushgw:9091",
            job="zombie-cleanup",
            registry=registry,
        )

    @patch("zombie_metrics.push_to_gateway", side_effect=ConnectionError("refused"))
    def test_failure_does_not_raise(self, mock_push):
        # Must not raise
        push_metrics("http://pushgw:9091")

    @patch("zombie_metrics.push_to_gateway", side_effect=OSError("network error"))
    def test_failure_logs_warning(self, mock_push, caplog):
        with caplog.at_level(logging.WARNING, logger="zombie-cleanup"):
            push_metrics("http://pushgw:9091")
        assert "Failed to push metrics" in caplog.text
        assert "network error" in caplog.text

    @patch("zombie_metrics.push_to_gateway")
    def test_success_logs_info(self, mock_push, caplog):
        with caplog.at_level(logging.INFO, logger="zombie-cleanup"):
            push_metrics("http://pushgw:9091")
        assert "Metrics pushed to http://pushgw:9091" in caplog.text
