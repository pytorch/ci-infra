"""Unit tests for smoke test helper functions."""

import sys
import time
from pathlib import Path
from unittest.mock import patch

# helpers.py lives in tests/smoke/ — add it to sys.path for import
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "tests" / "smoke"))

from helpers import (
    BACKOFF_ATTEMPTS,
    POD_STARTUP_GRACE_SECONDS,
    _count_unstable_nodes,
    _parse_k8s_timestamp,
    assert_daemonset_healthy,
    loki_read_url,
    mimir_read_url,
    pod_within_startup_grace,
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


def _make_node(name, ready="True", creation_ts="2020-01-01T00:00:00Z", deletion_ts=None, labels=None):
    """Build a minimal K8s node dict for testing."""
    meta = {"name": name, "creationTimestamp": creation_ts}
    if deletion_ts:
        meta["deletionTimestamp"] = deletion_ts
    if labels:
        meta["labels"] = labels
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
        # Creation timestamp = 10 seconds ago (< MIN_NODE_AGE_SECONDS=600)
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

    @patch("helpers.retry.time.sleep")
    @patch("helpers.k8s_asserts.run_kubectl")
    def test_zero_zero_allow_zero_false(self, mock_kubectl, mock_sleep):
        ds = _make_ds_data("my-ds", "kube-system", 0, 0)
        nodes = _make_nodes_data()
        # 0/0 with no selector → _has_matching_nodes returns True (conservative);
        # drops into the retry loop. Stub kubectl to keep returning 0/0 so the
        # retry budget exhausts and the assertion fails.
        mock_kubectl.side_effect = [
            {"status": {"desiredNumberScheduled": 0, "numberReady": 0}},
            _make_nodes_data(),
        ] * (BACKOFF_ATTEMPTS + 1)

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

    @patch("helpers.retry.time.sleep")
    @patch("helpers.k8s_asserts.run_kubectl")
    def test_mismatch_exceeds_unstable_retries_then_fails(self, mock_kubectl, mock_sleep):
        # desired=5, ready=2 → gap=3, unstable=1 → exceeds, enters retry loop
        ds = _make_ds_data("my-ds", "kube-system", 5, 2)
        nodes = _make_nodes_data(unstable_count=1, stable_count=4)

        # Each retry refresh re-fetches the DaemonSet then the node list.
        # Need BACKOFF_ATTEMPTS - 1 refresh cycles (no refresh after the
        # final failed attempt). Pad generously so we never run out.
        fresh_ds = {"status": {"desiredNumberScheduled": 5, "numberReady": 2}}
        fresh_nodes = _make_nodes_data(unstable_count=1, stable_count=4)
        mock_kubectl.side_effect = [fresh_ds, fresh_nodes] * (BACKOFF_ATTEMPTS + 1)

        import pytest

        with pytest.raises(AssertionError):
            assert_daemonset_healthy(ds, nodes, namespace="kube-system", name="my-ds")

    def test_negative_gap_with_unstable_nodes(self):
        # ready > desired → gap is negative, max(0, -1) = 0 <= unstable → passes
        ds = _make_ds_data("my-ds", "kube-system", 3, 4)
        nodes = _make_nodes_data(unstable_count=2, stable_count=3)
        assert_daemonset_healthy(ds, nodes, namespace="kube-system", name="my-ds")

    @patch("helpers.retry.time.sleep")
    @patch("helpers.k8s_asserts.run_kubectl")
    def test_zero_desired_with_eligible_nodes_retries_until_controller_catches_up(self, mock_kubectl, mock_sleep):
        # Post-deploy transient: DaemonSet still shows 0/0 but matching nodes
        # exist. Previously terminal-failed; now retries and succeeds once the
        # controller observes the eligible nodes and scales desired up.
        ds = _make_ds_data("my-ds", "kube-system", 0, 0)
        labels = {"workload-type": "github-runner"}
        nodes = {"items": [_make_node("n1", labels=labels)]}

        # First refresh still 0/0, second refresh shows 1/1 (controller caught up).
        mock_kubectl.side_effect = [
            {"status": {"desiredNumberScheduled": 0, "numberReady": 0}},
            {"items": [_make_node("n1", labels=labels)]},
            {"status": {"desiredNumberScheduled": 1, "numberReady": 1}},
            {"items": [_make_node("n1", labels=labels)]},
        ]

        assert_daemonset_healthy(
            ds,
            nodes,
            namespace="kube-system",
            name="my-ds",
            node_selector={"workload-type": ["github-runner"]},
        )

    @patch("helpers.retry.time.sleep")
    @patch("helpers.k8s_asserts.run_kubectl")
    def test_zero_desired_with_eligible_nodes_fails_after_retries(self, mock_kubectl, mock_sleep):
        # Same transient shape but the controller never catches up — must fail
        # loudly after the retry budget. Confirms the new retry path doesn't
        # silently mask persistent 0-desired bugs.
        ds = _make_ds_data("my-ds", "kube-system", 0, 0)
        labels = {"workload-type": "github-runner"}
        nodes = {"items": [_make_node("n1", labels=labels)]}

        fresh_ds = {"status": {"desiredNumberScheduled": 0, "numberReady": 0}}
        fresh_nodes = {"items": [_make_node("n1", labels=labels)]}
        mock_kubectl.side_effect = [fresh_ds, fresh_nodes] * (BACKOFF_ATTEMPTS + 1)

        import pytest

        with pytest.raises(AssertionError, match="0 desired pods"):
            assert_daemonset_healthy(
                ds,
                nodes,
                namespace="kube-system",
                name="my-ds",
                node_selector={"workload-type": ["github-runner"]},
            )

    def test_zero_desired_with_no_eligible_nodes_terminal_passes(self):
        # node_selector has no matches → no nodes to schedule on → 0/0 is
        # legitimately healthy, no retry, no failure. Guards the cheap-exit
        # path so we don't poll for ~10 minutes on scaled-to-zero pools.
        ds = _make_ds_data("my-ds", "kube-system", 0, 0)
        # Stable node but with a DIFFERENT label → no match.
        nodes = {"items": [_make_node("n1", labels={"workload-type": "other"})]}
        assert_daemonset_healthy(
            ds,
            nodes,
            namespace="kube-system",
            name="my-ds",
            node_selector={"workload-type": ["github-runner"]},
        )


# ---------------------------------------------------------------------------
# pod_within_startup_grace
# ---------------------------------------------------------------------------


def _make_pod(creation_ts: str | None = "2020-01-01T00:00:00Z") -> dict:
    """Build a minimal pod dict for testing."""
    meta: dict = {}
    if creation_ts is not None:
        meta["creationTimestamp"] = creation_ts
    return {"metadata": meta, "status": {}, "spec": {}}


class TestPodWithinStartupGrace:
    def test_old_pod_outside_grace(self):
        # Created in 2020, far older than POD_STARTUP_GRACE_SECONDS.
        assert pod_within_startup_grace(_make_pod()) is False

    def test_young_pod_inside_grace(self):
        # 60s ago — well within the default 15-min window.
        recent_ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 60))
        assert pod_within_startup_grace(_make_pod(recent_ts)) is True

    def test_pod_just_outside_grace(self):
        # Just past the grace window — must NOT be tolerated.
        past_ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - POD_STARTUP_GRACE_SECONDS - 10))
        assert pod_within_startup_grace(_make_pod(past_ts)) is False

    def test_pod_no_creation_timestamp(self):
        # Missing timestamp → can't compute age → conservative False.
        assert pod_within_startup_grace(_make_pod(creation_ts=None)) is False

    def test_custom_grace_seconds(self):
        # 30s ago — outside a 10s custom grace.
        recent_ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 30))
        assert pod_within_startup_grace(_make_pod(recent_ts), grace_seconds=10) is False
        assert pod_within_startup_grace(_make_pod(recent_ts), grace_seconds=60) is True
