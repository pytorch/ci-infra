"""Unit tests for capacity reservation feature.

Tests select_reserved_nodes (packing), reservation annotation management
(reservations), and compute_taints integration with reserved nodes.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from lightkube import ApiError
from models import (
    ANNOTATION_CAPACITY_RESERVED,
    ANNOTATION_DO_NOT_DISRUPT,
    Config,
    NodeState,
    PodInfo,
)
from packing import compute_taints, select_reserved_nodes
from reservations import (
    apply_reservation,
    cleanup_reservations,
    reconcile_reservations,
    remove_reservation,
)

# ============================================================================
# Helpers
# ============================================================================

NOW = datetime.now(UTC)
GiB = 1024**3


def make_config(**overrides) -> Config:
    defaults = {
        "interval": 20,
        "max_uptime_hours": 48,
        "nodepool_label": "osdc.io/node-compactor",
        "taint_key": "node-compactor.osdc.io/consolidating",
        "min_nodes": 1,
        "dry_run": False,
        "taint_cooldown": 300,
        "min_node_age": 900,
        "fleet_cooldown": 900,
        "taint_rate": 1.0,
        "spare_capacity_nodes": 0,
        "spare_capacity_ratio": 0.0,
        "spare_capacity_threshold": 0.4,
        "capacity_reservation_nodes": 0,
    }
    defaults.update(overrides)
    return Config(**defaults)


def make_node(
    name: str,
    nodepool: str = "default",
    cpu: float = 16.0,
    mem: int = 64 * GiB,
    is_tainted: bool = False,
    is_reserved: bool = False,
    creation_time: datetime | None = None,
) -> NodeState:
    return NodeState(
        name=name,
        nodepool=nodepool,
        allocatable_cpu=cpu,
        allocatable_memory=mem,
        creation_time=creation_time or NOW - timedelta(hours=1),
        is_tainted=is_tainted,
        is_reserved=is_reserved,
    )


def make_pod(
    name: str = "pod",
    cpu: float = 1.0,
    mem: int = 4 * GiB,
    node_name: str = "node-1",
    is_daemonset: bool = False,
    start_time: datetime | None = None,
) -> PodInfo:
    return PodInfo(
        name=name,
        namespace="default",
        cpu_request=cpu,
        memory_request=mem,
        node_name=node_name,
        is_daemonset=is_daemonset,
        start_time=start_time,
    )


def _add_workload(node: NodeState, cpu: float, mem: int) -> None:
    """Add a workload pod to a node to set its utilization."""
    pod = make_pod(
        name=f"workload-{node.name}",
        cpu=cpu,
        mem=mem,
        node_name=node.name,
        start_time=NOW - timedelta(minutes=30),
    )
    node.pods.append(pod)


def _make_api_error(code: int) -> ApiError:
    """Create a mock ApiError with the given status code."""
    resp = MagicMock()
    err = ApiError(response=resp)
    err.status = MagicMock(code=code)
    return err


def _mock_node(name: str, annotations: dict | None = None, resource_version: str = "1"):
    """Create a mock Kubernetes Node object for API mocking."""
    node = MagicMock()
    node.metadata.name = name
    node.metadata.annotations = annotations or {}
    node.metadata.resourceVersion = resource_version
    return node


# ============================================================================
# select_reserved_nodes tests
# ============================================================================


class TestSelectReservedNodes:
    """Tests for select_reserved_nodes in packing.py."""

    def test_disabled_when_zero(self):
        """Returns empty dict when capacity_reservation_nodes=0."""
        cfg = make_config(capacity_reservation_nodes=0)
        pool_nodes = {"pool": [make_node("n-0")]}
        assert select_reserved_nodes(pool_nodes, cfg) == {}

    def test_selects_young_nodes_only(self):
        """Old nodes (>= max_uptime_hours) are excluded."""
        cfg = make_config(capacity_reservation_nodes=2, max_uptime_hours=48)
        young = make_node("young", creation_time=NOW - timedelta(hours=1))
        old = make_node("old", creation_time=NOW - timedelta(hours=49))
        pool_nodes = {"pool": [young, old]}

        result = select_reserved_nodes(pool_nodes, cfg)
        assert result == {"pool": {"young"}}

    def test_no_young_nodes_empty(self):
        """Empty result when all nodes are too old."""
        cfg = make_config(capacity_reservation_nodes=2, max_uptime_hours=48)
        old1 = make_node("old1", creation_time=NOW - timedelta(hours=50))
        old2 = make_node("old2", creation_time=NOW - timedelta(hours=60))
        pool_nodes = {"pool": [old1, old2]}

        result = select_reserved_nodes(pool_nodes, cfg)
        assert result == {}

    def test_sort_lowest_utilization_first(self):
        """Nodes with lowest utilization are selected first."""
        cfg = make_config(capacity_reservation_nodes=1)
        low = make_node("low")
        high = make_node("high")
        _add_workload(high, cpu=12.0, mem=48 * GiB)
        pool_nodes = {"pool": [high, low]}

        result = select_reserved_nodes(pool_nodes, cfg)
        assert result == {"pool": {"low"}}

    def test_count_limited(self):
        """Only selects up to capacity_reservation_nodes per pool."""
        cfg = make_config(capacity_reservation_nodes=2)
        nodes = [make_node(f"n-{i}") for i in range(5)]
        pool_nodes = {"pool": nodes}

        result = select_reserved_nodes(pool_nodes, cfg)
        assert len(result["pool"]) == 2

    def test_per_pool_selection(self):
        """Each pool gets independent selection."""
        cfg = make_config(capacity_reservation_nodes=1)
        pool_nodes = {
            "pool-a": [make_node("a-0", nodepool="pool-a")],
            "pool-b": [make_node("b-0", nodepool="pool-b"), make_node("b-1", nodepool="pool-b")],
        }

        result = select_reserved_nodes(pool_nodes, cfg)
        assert "pool-a" in result
        assert "pool-b" in result
        assert len(result["pool-a"]) == 1
        assert len(result["pool-b"]) == 1

    def test_tiebreak_by_uptime(self):
        """When utilization and pod age are equal, the node with highest
        uptime is selected first (sort key uses -uptime_seconds)."""
        cfg = make_config(capacity_reservation_nodes=1)
        older = make_node("older", creation_time=NOW - timedelta(hours=10))
        newer = make_node("newer", creation_time=NOW - timedelta(hours=1))
        pool_nodes = {"pool": [older, newer]}

        result = select_reserved_nodes(pool_nodes, cfg)
        # Sort key: (util, -youngest_pod_age, -uptime_seconds)
        # older has higher uptime -> -uptime is more negative -> sorts first
        assert result == {"pool": {"older"}}


# ============================================================================
# apply_reservation tests
# ============================================================================


class TestApplyReservation:
    """Tests for apply_reservation in reservations.py."""

    def test_dry_run(self):
        """Dry run returns True without API call."""
        client = MagicMock()
        assert apply_reservation(client, "node-0", dry_run=True) is True
        client.patch.assert_not_called()

    def test_success(self):
        """Patches node with both annotations."""
        client = MagicMock()
        assert apply_reservation(client, "node-0", dry_run=False) is True
        client.patch.assert_called_once()
        patch_data = client.patch.call_args[0][2]
        annotations = patch_data["metadata"]["annotations"]
        assert annotations[ANNOTATION_CAPACITY_RESERVED] == "true"
        assert annotations[ANNOTATION_DO_NOT_DISRUPT] == "true"

    def test_node_404(self):
        """Returns False when node has been deleted."""
        client = MagicMock()
        client.patch.side_effect = _make_api_error(404)
        assert apply_reservation(client, "node-0", dry_run=False) is False

    def test_api_error_other(self):
        """Returns False on non-404 API errors."""
        client = MagicMock()
        client.patch.side_effect = _make_api_error(500)
        assert apply_reservation(client, "node-0", dry_run=False) is False


# ============================================================================
# remove_reservation tests
# ============================================================================


class TestRemoveReservation:
    """Tests for remove_reservation in reservations.py."""

    def test_dry_run(self):
        """Dry run returns True without API call."""
        client = MagicMock()
        assert remove_reservation(client, "node-0", dry_run=True) is True
        client.get.assert_not_called()

    def test_success_with_ownership(self):
        """Removes both annotations when our marker is present."""
        client = MagicMock()
        node = _mock_node(
            "node-0",
            {
                ANNOTATION_CAPACITY_RESERVED: "true",
                ANNOTATION_DO_NOT_DISRUPT: "true",
                "other-annotation": "keep",
            },
        )
        client.get.return_value = node

        assert remove_reservation(client, "node-0", dry_run=False) is True
        client.patch.assert_called_once()
        patch_data = client.patch.call_args[0][2]
        new_annotations = patch_data["metadata"]["annotations"]
        assert ANNOTATION_CAPACITY_RESERVED not in new_annotations
        assert ANNOTATION_DO_NOT_DISRUPT not in new_annotations
        assert new_annotations["other-annotation"] == "keep"

    def test_skips_without_ownership(self):
        """Returns True but no patch when our marker is absent."""
        client = MagicMock()
        node = _mock_node("node-0", {ANNOTATION_DO_NOT_DISRUPT: "true"})
        client.get.return_value = node

        assert remove_reservation(client, "node-0", dry_run=False) is True
        client.patch.assert_not_called()

    def test_conflict_retry(self):
        """Retries on 409 Conflict, succeeds on second attempt."""
        client = MagicMock()
        node = _mock_node(
            "node-0",
            {
                ANNOTATION_CAPACITY_RESERVED: "true",
                ANNOTATION_DO_NOT_DISRUPT: "true",
            },
        )
        client.get.return_value = node
        client.patch.side_effect = [_make_api_error(409), None]

        assert remove_reservation(client, "node-0", dry_run=False) is True
        assert client.patch.call_count == 2
        assert client.get.call_count == 2

    def test_node_404_on_get(self):
        """Returns True when node disappears during get."""
        client = MagicMock()
        client.get.side_effect = _make_api_error(404)

        assert remove_reservation(client, "node-0", dry_run=False) is True
        client.patch.assert_not_called()

    def test_node_404_on_patch(self):
        """Returns True when node disappears during patch."""
        client = MagicMock()
        node = _mock_node(
            "node-0",
            {
                ANNOTATION_CAPACITY_RESERVED: "true",
                ANNOTATION_DO_NOT_DISRUPT: "true",
            },
        )
        client.get.return_value = node
        client.patch.side_effect = _make_api_error(404)

        assert remove_reservation(client, "node-0", dry_run=False) is True

    def test_max_retries_exhausted(self):
        """Returns False after all 409 retries are exhausted."""
        client = MagicMock()
        node = _mock_node(
            "node-0",
            {
                ANNOTATION_CAPACITY_RESERVED: "true",
                ANNOTATION_DO_NOT_DISRUPT: "true",
            },
        )
        client.get.return_value = node
        client.patch.side_effect = _make_api_error(409)

        result = remove_reservation(client, "node-0", dry_run=False, max_retries=3)
        assert result is False
        assert client.patch.call_count == 3
        assert client.get.call_count == 3

    def test_non_404_error_on_get_raises(self):
        """Re-raises non-404 ApiError from get."""
        client = MagicMock()
        client.get.side_effect = _make_api_error(500)

        with pytest.raises(ApiError):
            remove_reservation(client, "node-0", dry_run=False)

    def test_non_409_non_404_error_on_patch_raises(self):
        """Re-raises non-409/non-404 ApiError from patch."""
        client = MagicMock()
        node = _mock_node(
            "node-0",
            {
                ANNOTATION_CAPACITY_RESERVED: "true",
                ANNOTATION_DO_NOT_DISRUPT: "true",
            },
        )
        client.get.return_value = node
        client.patch.side_effect = _make_api_error(500)

        with pytest.raises(ApiError):
            remove_reservation(client, "node-0", dry_run=False)

    def test_empty_annotations_after_removal(self):
        """Sets annotations to None when only our two keys existed."""
        client = MagicMock()
        node = _mock_node(
            "node-0",
            {
                ANNOTATION_CAPACITY_RESERVED: "true",
                ANNOTATION_DO_NOT_DISRUPT: "true",
            },
        )
        client.get.return_value = node

        assert remove_reservation(client, "node-0", dry_run=False) is True
        patch_data = client.patch.call_args[0][2]
        # When all annotations are removed, the value is None (not empty dict)
        assert patch_data["metadata"]["annotations"] is None


# ============================================================================
# reconcile_reservations tests
# ============================================================================


class TestReconcileReservations:
    """Tests for reconcile_reservations in reservations.py."""

    @patch("reservations.apply_reservation", return_value=True)
    @patch("reservations.remove_reservation", return_value=True)
    def test_adds_new_reservations(self, mock_remove, mock_apply):
        """Calls apply_reservation for newly desired nodes."""
        client = MagicMock()
        node_states = [
            make_node("n-0"),  # not reserved, should be added
            make_node("n-1"),
        ]

        result = reconcile_reservations(client, node_states, {"n-0"}, dry_run=False)
        mock_apply.assert_called_once_with(client, "n-0", False)
        mock_remove.assert_not_called()
        assert result == {"added": ["n-0"], "removed": []}

    @patch("reservations.apply_reservation", return_value=True)
    @patch("reservations.remove_reservation", return_value=True)
    def test_removes_old_reservations(self, mock_remove, mock_apply):
        """Calls remove_reservation for no-longer-desired nodes."""
        client = MagicMock()
        node_states = [
            make_node("n-0", is_reserved=True),  # reserved, should be removed
            make_node("n-1"),
        ]

        result = reconcile_reservations(client, node_states, set(), dry_run=False)
        mock_apply.assert_not_called()
        mock_remove.assert_called_once_with(client, "n-0", False)
        assert result == {"added": [], "removed": ["n-0"]}

    @patch("reservations.apply_reservation", return_value=True)
    @patch("reservations.remove_reservation", return_value=True)
    def test_no_changes(self, mock_remove, mock_apply):
        """No API calls when current matches desired."""
        client = MagicMock()
        node_states = [
            make_node("n-0", is_reserved=True),
        ]

        result = reconcile_reservations(client, node_states, {"n-0"}, dry_run=False)
        mock_apply.assert_not_called()
        mock_remove.assert_not_called()
        assert result == {"added": [], "removed": []}


# ============================================================================
# cleanup_reservations tests
# ============================================================================


class TestCleanupReservations:
    """Tests for cleanup_reservations in reservations.py."""

    @patch("reservations.remove_reservation", return_value=True)
    def test_cleans_all_reserved(self, mock_remove):
        """Removes reservations from all nodes with our marker."""
        client = MagicMock()
        reserved_node = _mock_node("n-0", {ANNOTATION_CAPACITY_RESERVED: "true"})
        plain_node = _mock_node("n-1", {})
        client.list.return_value = [reserved_node, plain_node]

        cleanup_reservations(client)
        mock_remove.assert_called_once_with(client, "n-0", dry_run=False)

    @patch("reservations.remove_reservation", return_value=True)
    def test_skips_unreserved(self, mock_remove):
        """Doesn't call remove on nodes without our marker."""
        client = MagicMock()
        # Node with do-not-disrupt but NOT our marker — not ours to remove
        node = _mock_node("n-0", {ANNOTATION_DO_NOT_DISRUPT: "true"})
        client.list.return_value = [node]

        cleanup_reservations(client)
        mock_remove.assert_not_called()


# ============================================================================
# compute_taints integration with reserved_nodes
# ============================================================================


class TestComputeTaintsWithReservation:
    """Tests that reserved_nodes are handled correctly by compute_taints."""

    def test_reserved_excluded_from_taint(self):
        """Reserved nodes are never tainted even if surplus allows it."""
        cfg = make_config(min_nodes=1, capacity_reservation_nodes=1)
        nodes = {}
        for i in range(5):
            n = make_node(f"node-{i}", nodepool="pool")
            nodes[f"node-{i}"] = n

        reserved = {"node-0"}
        to_taint, _to_untaint, _mandatory, _rate = compute_taints(nodes, cfg, reserved_nodes=reserved)

        assert "node-0" not in to_taint

    def test_tainted_reserved_force_untainted(self):
        """A tainted node that becomes reserved is force-untainted."""
        cfg = make_config(min_nodes=1, capacity_reservation_nodes=1)
        nodes = {}
        for i in range(5):
            n = make_node(f"node-{i}", nodepool="pool", is_tainted=(i == 0))
            nodes[f"node-{i}"] = n

        reserved = {"node-0"}
        _to_taint, to_untaint, mandatory, _rate = compute_taints(nodes, cfg, reserved_nodes=reserved)

        assert "node-0" in to_untaint
        assert "node-0" in mandatory

    def test_reserved_count_toward_pool_size(self):
        """Reserved nodes still count in pool_nodes for surplus calculation.

        With 5 nodes and min_nodes=1, surplus=4. Reserving 1 node doesn't
        reduce the pool size, so surplus stays at 4. But one node can't be
        tainted (reserved), so max taints = 3 (from non-reserved surplus).
        """
        cfg = make_config(min_nodes=1, capacity_reservation_nodes=1)
        nodes = {}
        for i in range(5):
            n = make_node(f"node-{i}", nodepool="pool")
            nodes[f"node-{i}"] = n

        reserved = {"node-0"}
        to_taint, _untaint, _mandatory, _rate = compute_taints(nodes, cfg, reserved_nodes=reserved)

        # node-0 is reserved, 4 others eligible. surplus=4. All 4 non-reserved
        # can be tainted (no spare capacity constraint with defaults).
        assert "node-0" not in to_taint
        assert len(to_taint) <= 4

    def test_reserved_none_parameter(self):
        """Passing reserved_nodes=None works (feature disabled)."""
        cfg = make_config(min_nodes=1)
        nodes = {}
        for i in range(3):
            n = make_node(f"node-{i}", nodepool="pool")
            nodes[f"node-{i}"] = n

        to_taint, _untaint, _mandatory, _rate = compute_taints(nodes, cfg, reserved_nodes=None)
        # Should work normally — surplus=2, both can be tainted
        assert len(to_taint) == 2

    def test_all_reserved_no_taints(self):
        """When all nodes are reserved, none can be tainted."""
        cfg = make_config(min_nodes=1, capacity_reservation_nodes=3)
        nodes = {}
        for i in range(3):
            n = make_node(f"node-{i}", nodepool="pool")
            nodes[f"node-{i}"] = n

        reserved = {"node-0", "node-1", "node-2"}
        to_taint, _untaint, _mandatory, _rate = compute_taints(nodes, cfg, reserved_nodes=reserved)

        assert len(to_taint) == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
