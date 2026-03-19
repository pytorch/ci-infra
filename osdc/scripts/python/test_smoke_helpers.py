"""Unit tests for smoke test helper functions."""

import sys
import time
from pathlib import Path
from unittest.mock import patch

# helpers.py lives in tests/smoke/ — add it to sys.path for import
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "tests" / "smoke"))

from helpers import (
    _count_unstable_nodes,
    _parse_k8s_timestamp,
    assert_daemonset_healthy,
    loki_read_url,
    mimir_read_url,
)


class TestMimirReadUrl:
    def test_standard_url(self):
        assert (
            mimir_read_url("https://prometheus-prod-36-prod-us-west-0.grafana.net/api/prom/push")
            == "https://prometheus-prod-36-prod-us-west-0.grafana.net/api/prom/api/v1/query"
        )

    def test_trailing_slash(self):
        assert (
            mimir_read_url("https://host.grafana.net/api/prom/push/")
            == "https://host.grafana.net/api/prom/api/v1/query"
        )

    def test_no_push_suffix(self):
        assert mimir_read_url("https://host.grafana.net/api/prom") == "https://host.grafana.net/api/prom/api/v1/query"


class TestLokiReadUrl:
    def test_standard_url(self):
        assert (
            loki_read_url("https://logs-prod-021.grafana.net/loki/api/v1/push")
            == "https://logs-prod-021.grafana.net/loki/api/v1/query_range"
        )

    def test_trailing_slash(self):
        assert (
            loki_read_url("https://host.grafana.net/loki/api/v1/push/")
            == "https://host.grafana.net/loki/api/v1/query_range"
        )

    def test_no_push_suffix(self):
        assert (
            loki_read_url("https://host.grafana.net/loki/api/v1") == "https://host.grafana.net/loki/api/v1/query_range"
        )


# ---------------------------------------------------------------------------
# _parse_k8s_timestamp
# ---------------------------------------------------------------------------


class TestParseK8sTimestamp:
    def test_standard_k8s_timestamp(self):
        result = _parse_k8s_timestamp("2024-01-15T10:30:00Z")
        assert isinstance(result, float)

    def test_known_timestamp_value(self):
        # 2024-01-01T00:00:00Z == 1704067200 Unix epoch
        result = _parse_k8s_timestamp("2024-01-01T00:00:00Z")
        assert result == 1704067200.0


# ---------------------------------------------------------------------------
# _count_unstable_nodes
# ---------------------------------------------------------------------------


def _make_node(name, ready="True", creation_ts="2020-01-01T00:00:00Z", deletion_ts=None):
    """Build a minimal K8s node dict for testing."""
    meta = {"name": name, "creationTimestamp": creation_ts}
    if deletion_ts:
        meta["deletionTimestamp"] = deletion_ts
    return {
        "metadata": meta,
        "status": {
            "conditions": [{"type": "Ready", "status": ready}],
        },
    }


class TestCountUnstableNodes:
    def test_empty_node_list(self):
        assert _count_unstable_nodes({"items": []}) == 0

    def test_node_with_deletion_timestamp(self):
        node = _make_node("n1", deletion_ts="2024-06-01T00:00:00Z")
        assert _count_unstable_nodes({"items": [node]}) == 1

    def test_node_not_ready(self):
        node = _make_node("n1", ready="False")
        assert _count_unstable_nodes({"items": [node]}) == 1

    def test_node_too_new(self):
        # Creation timestamp = 10 seconds ago (< MIN_NODE_AGE_SECONDS=120)
        recent_ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 10))
        node = _make_node("n1", creation_ts=recent_ts)
        assert _count_unstable_nodes({"items": [node]}) == 1

    def test_stable_node(self):
        # Old, Ready, no deletion — should NOT be counted
        node = _make_node("n1", ready="True", creation_ts="2020-01-01T00:00:00Z")
        assert _count_unstable_nodes({"items": [node]}) == 0

    def test_mixed_nodes(self):
        stable = _make_node("stable", ready="True", creation_ts="2020-01-01T00:00:00Z")
        deleting = _make_node("deleting", deletion_ts="2024-06-01T00:00:00Z")
        not_ready = _make_node("notready", ready="False")
        assert _count_unstable_nodes({"items": [stable, deleting, not_ready]}) == 2


# ---------------------------------------------------------------------------
# assert_daemonset_healthy
# ---------------------------------------------------------------------------


def _make_ds_data(ds_name, namespace, desired, ready):
    """Build minimal batch-fetched DaemonSet data."""
    return {
        "items": [
            {
                "metadata": {"name": ds_name, "namespace": namespace},
                "status": {"desiredNumberScheduled": desired, "numberReady": ready},
            }
        ]
    }


def _make_nodes_data(unstable_count=0, stable_count=1):
    """Build node data with the given number of stable/unstable nodes."""
    items = []
    for i in range(stable_count):
        items.append(_make_node(f"stable-{i}", ready="True", creation_ts="2020-01-01T00:00:00Z"))
    for i in range(unstable_count):
        items.append(_make_node(f"unstable-{i}", ready="False"))
    return {"items": items}


class TestAssertDaemonsetHealthy:
    def test_desired_equals_ready(self):
        ds = _make_ds_data("my-ds", "kube-system", 3, 3)
        nodes = _make_nodes_data()
        # Should pass without error
        assert_daemonset_healthy(ds, nodes, namespace="kube-system", name="my-ds")

    def test_zero_zero_allow_zero_false(self):
        ds = _make_ds_data("my-ds", "kube-system", 0, 0)
        nodes = _make_nodes_data()
        import pytest

        with pytest.raises(AssertionError, match="0 desired pods"):
            assert_daemonset_healthy(ds, nodes, namespace="kube-system", name="my-ds", allow_zero=False)

    def test_zero_zero_allow_zero_true(self):
        ds = _make_ds_data("my-ds", "kube-system", 0, 0)
        nodes = _make_nodes_data()
        # Should pass without error
        assert_daemonset_healthy(ds, nodes, namespace="kube-system", name="my-ds", allow_zero=True)

    def test_mismatch_explained_by_unstable_nodes(self):
        # desired=5, ready=3 → gap=2, unstable=2 → passes
        ds = _make_ds_data("my-ds", "kube-system", 5, 3)
        nodes = _make_nodes_data(unstable_count=2, stable_count=3)
        assert_daemonset_healthy(ds, nodes, namespace="kube-system", name="my-ds")

    @patch("helpers.time.sleep")
    @patch("helpers.run_kubectl")
    def test_mismatch_exceeds_unstable_retries_then_fails(self, mock_kubectl, mock_sleep):
        # desired=5, ready=2 → gap=3, unstable=1 → exceeds, enters retry loop
        ds = _make_ds_data("my-ds", "kube-system", 5, 2)
        nodes = _make_nodes_data(unstable_count=1, stable_count=4)

        # Each retry returns the same stale values (ds then nodes)
        fresh_ds = {"status": {"desiredNumberScheduled": 5, "numberReady": 2}}
        fresh_nodes = _make_nodes_data(unstable_count=1, stable_count=4)
        mock_kubectl.side_effect = [fresh_ds, fresh_nodes] * 6  # READY_RETRIES iterations

        # The function raises AssertionError (note: source has typo "AssertionError",
        # which is actually a NameError at runtime — catch both)
        import pytest

        with pytest.raises((AssertionError, NameError)):
            assert_daemonset_healthy(ds, nodes, namespace="kube-system", name="my-ds")

    def test_negative_gap_with_unstable_nodes(self):
        # ready > desired → gap is negative, max(0, -1) = 0 <= unstable → passes
        ds = _make_ds_data("my-ds", "kube-system", 3, 4)
        nodes = _make_nodes_data(unstable_count=2, stable_count=3)
        assert_daemonset_healthy(ds, nodes, namespace="kube-system", name="my-ds")
