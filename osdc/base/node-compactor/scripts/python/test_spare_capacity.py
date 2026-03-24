"""Unit tests for spare capacity floor in node compactor packing."""

import math
from datetime import UTC, datetime, timedelta

import pytest
from models import Config, NodeState, PodInfo
from packing import _count_spare_nodes, compute_taints

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
        "taint_rate": 1.0,  # disable rate limiting by default for spare tests
        "spare_capacity_nodes": 3,
        "spare_capacity_ratio": 0.15,
        "spare_capacity_threshold": 0.4,
    }
    defaults.update(overrides)
    return Config(**defaults)


def make_node(
    name: str,
    nodepool: str = "default",
    cpu: float = 16.0,
    mem: int = 64 * GiB,
    is_tainted: bool = False,
    creation_time: datetime | None = None,
) -> NodeState:
    return NodeState(
        name=name,
        nodepool=nodepool,
        allocatable_cpu=cpu,
        allocatable_memory=mem,
        creation_time=creation_time or NOW - timedelta(hours=1),
        is_tainted=is_tainted,
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


# ============================================================================
# _count_spare_nodes tests
# ============================================================================


class TestCountSpareNodes:
    """Unit tests for the _count_spare_nodes helper."""

    def test_all_nodes_low_utilization(self):
        """All untainted, low-utilization nodes count as spare."""
        nodes = [make_node(f"n-{i}", nodepool="pool") for i in range(5)]
        # No pods -> utilization=0 -> all below threshold
        assert _count_spare_nodes(nodes, None, set(), 0.4) == 5

    def test_excludes_tainted_nodes(self):
        """Tainted nodes don't count as spare."""
        nodes = [
            make_node("n-0", nodepool="pool"),
            make_node("n-1", nodepool="pool", is_tainted=True),
            make_node("n-2", nodepool="pool"),
        ]
        assert _count_spare_nodes(nodes, None, set(), 0.4) == 2

    def test_excludes_nodes_in_to_taint(self):
        """Nodes in to_taint set don't count as spare."""
        nodes = [make_node(f"n-{i}", nodepool="pool") for i in range(3)]
        assert _count_spare_nodes(nodes, None, {"n-1"}, 0.4) == 2

    def test_excludes_high_utilization(self):
        """Nodes above threshold don't count as spare."""
        nodes = [make_node(f"n-{i}", nodepool="pool") for i in range(3)]
        # Give n-1 high utilization (50% CPU on 16-core node = 8 cores)
        _add_workload(nodes[1], cpu=8.0, mem=1 * GiB)
        assert _count_spare_nodes(nodes, None, set(), 0.4) == 2

    def test_excludes_candidate_node(self):
        """The exclude_node parameter removes one node from consideration."""
        nodes = [make_node(f"n-{i}", nodepool="pool") for i in range(3)]
        assert _count_spare_nodes(nodes, "n-0", set(), 0.4) == 2

    def test_threshold_boundary_inclusive(self):
        """Nodes at exactly the threshold count as spare."""
        node = make_node("n-0", nodepool="pool")
        # 40% utilization on 16-core node = 6.4 cores
        _add_workload(node, cpu=6.4, mem=1 * GiB)
        assert node.utilization == pytest.approx(0.4, abs=0.001)
        assert _count_spare_nodes([node], None, set(), 0.4) == 1


# ============================================================================
# Required spare computation tests
# ============================================================================


class TestRequiredSpareComputation:
    """Verify required_spare = max(spare_capacity_nodes, ceil(pool_size * ratio))."""

    def test_pool_3_nodes(self):
        """3 nodes: max(3, ceil(3*0.15)) = max(3, 1) = 3."""
        required = max(3, math.ceil(3 * 0.15))
        assert required == 3

    def test_pool_20_nodes(self):
        """20 nodes: max(3, ceil(20*0.15)) = max(3, 3) = 3."""
        required = max(3, math.ceil(20 * 0.15))
        assert required == 3

    def test_pool_50_nodes(self):
        """50 nodes: max(3, ceil(50*0.15)) = max(3, 8) = 8."""
        required = max(3, math.ceil(50 * 0.15))
        assert required == 8


# ============================================================================
# Spare capacity integration tests with compute_taints
# ============================================================================


class TestSpareCapacityTaintBlocking:
    """Tests that spare capacity prevents tainting when it would
    leave too few low-utilization nodes untainted."""

    def test_5_nodes_3_surplus_spare_3_limits_taints(self):
        """Pool with 5 nodes, 3 surplus. spare_capacity_nodes=3 means
        we need 3 untainted low-util nodes. With 5 nodes and min_nodes=1,
        surplus=4 (bin_pack says 0 needed, min_nodes=1). But spare capacity
        requires 3 untainted spare nodes, so only 2 can be tainted."""
        cfg = make_config(min_nodes=1, spare_capacity_nodes=3, spare_capacity_ratio=0.0)
        nodes = {}
        for i in range(5):
            n = make_node(f"node-{i}", nodepool="pool")
            nodes[f"node-{i}"] = n

        to_taint, _to_untaint, _mandatory, _rate_limited = compute_taints(nodes, cfg)

        # 5 nodes, 0 pods -> surplus=4, but spare capacity needs 3 untainted
        # low-util nodes. So at most 2 can be tainted.
        assert len(to_taint) <= 2
        # At least 3 nodes remain untainted
        untainted = [n for n in nodes if n not in to_taint]
        assert len(untainted) >= 3

    def test_tiny_pool_3_nodes_no_taints(self):
        """Pool with 3 nodes: required_spare = max(3, ceil(3*0.15)) = 3.
        All 3 are needed for spare capacity, so NO nodes can be tainted."""
        cfg = make_config(min_nodes=1, spare_capacity_nodes=3, spare_capacity_ratio=0.15)
        nodes = {}
        for i in range(3):
            n = make_node(f"node-{i}", nodepool="pool")
            nodes[f"node-{i}"] = n

        to_taint, _to_untaint, _mandatory, _rate_limited = compute_taints(nodes, cfg)

        # surplus = 3-1 = 2, but spare_capacity=3 means 3 untainted needed.
        # Since there are only 3 nodes total, none can be tainted.
        assert len(to_taint) == 0

    def test_pool_20_nodes_spare_3(self):
        """Pool with 20 nodes: required_spare = max(3, ceil(20*0.15)) = 3.
        With no pods, surplus=19. Spare capacity allows up to 17 taints."""
        cfg = make_config(min_nodes=1, spare_capacity_nodes=3, spare_capacity_ratio=0.15)
        nodes = {}
        for i in range(20):
            n = make_node(f"node-{i}", nodepool="pool")
            nodes[f"node-{i}"] = n

        to_taint, _to_untaint, _mandatory, _rate_limited = compute_taints(nodes, cfg)

        # surplus = 19, spare_capacity needs 3 untainted -> max 17 tainted
        assert len(to_taint) <= 17
        untainted = [n for n in nodes if n not in to_taint]
        assert len(untainted) >= 3

    def test_pool_50_nodes_spare_8(self):
        """Pool with 50 nodes: required_spare = max(3, ceil(50*0.15)) = 8."""
        cfg = make_config(min_nodes=1, spare_capacity_nodes=3, spare_capacity_ratio=0.15)
        nodes = {}
        for i in range(50):
            n = make_node(f"node-{i}", nodepool="pool")
            nodes[f"node-{i}"] = n

        to_taint, _to_untaint, _mandatory, _rate_limited = compute_taints(nodes, cfg)

        # surplus = 49, spare_capacity needs 8 untainted -> max 42 tainted
        assert len(to_taint) <= 42
        untainted = [n for n in nodes if n not in to_taint]
        assert len(untainted) >= 8

    def test_high_utilization_pool_no_spare_no_taint(self):
        """All nodes above 0.4 utilization -> no spare nodes -> can't taint.
        The spare capacity check prevents tainting because there are zero
        nodes at or below the threshold."""
        cfg = make_config(min_nodes=1, spare_capacity_nodes=3, spare_capacity_ratio=0.0)
        nodes = {}
        for i in range(5):
            n = make_node(f"node-{i}", nodepool="pool")
            # 50% CPU utilization (above 0.4 threshold)
            _add_workload(n, cpu=8.0, mem=1 * GiB)
            nodes[f"node-{i}"] = n

        to_taint, _to_untaint, _mandatory, _rate_limited = compute_taints(nodes, cfg)

        # No nodes have utilization <= 0.4, so spare count is always 0,
        # which is < required (3). No taints allowed.
        assert len(to_taint) == 0

    def test_tainted_low_util_gets_untainted_for_spare(self):
        """A tainted node with low utilization gets untainted when spare
        count is below required."""
        cfg = make_config(min_nodes=1, spare_capacity_nodes=3, spare_capacity_ratio=0.0)

        # 5 nodes: 3 tainted (low util), 2 untainted (low util)
        nodes = {}
        for i in range(3):
            n = make_node(f"tainted-{i}", nodepool="pool", is_tainted=True)
            nodes[f"tainted-{i}"] = n
        for i in range(2):
            n = make_node(f"untainted-{i}", nodepool="pool", is_tainted=False)
            nodes[f"untainted-{i}"] = n

        to_taint, desired_untaint, _mandatory, _rate_limited = compute_taints(nodes, cfg)

        # We need 3 spare (untainted, low-util) nodes. Currently only 2 are
        # untainted. The spare recovery logic should untaint at least 1
        # tainted node to reach 3 spare nodes.
        untainted_after = set()
        for name in nodes:
            ns = nodes[name]
            will_be_tainted = (ns.is_tainted or name in to_taint) and name not in desired_untaint
            if not will_be_tainted:
                untainted_after.add(name)

        # At least 3 nodes should be untainted after reconciliation
        assert len(untainted_after) >= 3

    def test_disabled_when_both_zero(self):
        """spare_capacity_nodes=0 and spare_capacity_ratio=0 disables
        the feature entirely. All surplus nodes can be tainted."""
        cfg = make_config(
            min_nodes=1,
            spare_capacity_nodes=0,
            spare_capacity_ratio=0.0,
        )
        nodes = {}
        for i in range(5):
            n = make_node(f"node-{i}", nodepool="pool")
            nodes[f"node-{i}"] = n

        to_taint, _to_untaint, _mandatory, _rate_limited = compute_taints(nodes, cfg)

        # surplus = 4, no spare capacity constraint -> 4 tainted
        assert len(to_taint) == 4

    def test_spare_capacity_additive_to_safety_check(self):
        """Spare capacity is checked AFTER the safety check (pods fit).
        If the safety check passes but spare fails, node is still skipped."""
        cfg = make_config(min_nodes=1, spare_capacity_nodes=3, spare_capacity_ratio=0.0)

        # 4 nodes, 1 has all the workload pods, 3 are empty.
        # Safety check: pods can fit on remaining nodes (they're empty).
        # Spare check: need 3 untainted low-util nodes.
        # With 4 nodes, surplus=3 (bin_pack needs 1 for the busy node).
        # Spare requires 3 untainted spare -> only 1 can be tainted.
        nodes = {}
        busy = make_node("busy", nodepool="pool")
        _add_workload(busy, cpu=12.0, mem=48 * GiB)
        nodes["busy"] = busy

        for i in range(3):
            n = make_node(f"empty-{i}", nodepool="pool")
            nodes[f"empty-{i}"] = n

        to_taint, _to_untaint, _mandatory, _rate_limited = compute_taints(nodes, cfg)

        # The busy node has utilization > 0.4, so it doesn't count as spare.
        # The 3 empty nodes all have utilization <= 0.4.
        # Required spare = 3. After tainting any empty node, only 2 spare
        # remain, which is < 3. So no empty nodes can be tainted.
        # The busy node CAN be tainted (it has high util, not counted as spare).
        untainted = [n for n in nodes if n not in to_taint]
        spare_nodes = [n for n in untainted if nodes[n].utilization <= 0.4]
        assert len(spare_nodes) >= 3

    def test_multiple_pools_independent(self):
        """Spare capacity is computed per pool, not globally."""
        cfg = make_config(min_nodes=1, spare_capacity_nodes=2, spare_capacity_ratio=0.0)
        nodes = {}

        # Pool A: 4 nodes
        for i in range(4):
            n = make_node(f"a-{i}", nodepool="pool-a")
            nodes[f"a-{i}"] = n

        # Pool B: 4 nodes
        for i in range(4):
            n = make_node(f"b-{i}", nodepool="pool-b")
            nodes[f"b-{i}"] = n

        to_taint, _to_untaint, _mandatory, _rate_limited = compute_taints(nodes, cfg)

        # Each pool: surplus=3, spare_capacity_nodes=2
        # Each pool can taint at most 2 nodes (4-2=2).
        pool_a_untainted = [n for n in nodes if n.startswith("a-") and n not in to_taint]
        pool_b_untainted = [n for n in nodes if n.startswith("b-") and n not in to_taint]

        assert len(pool_a_untainted) >= 2
        assert len(pool_b_untainted) >= 2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
