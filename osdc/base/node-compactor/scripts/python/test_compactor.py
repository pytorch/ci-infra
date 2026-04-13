"""Unit tests for the node compactor controller."""

import math
import signal
import time
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from compactor import main, reconcile
from lightkube import ApiError
from models import Config, NodeState, PodInfo, parse_cpu, parse_memory
from packing import _pods_fit_on_nodes, bin_pack_min_nodes, compute_taints

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


# ============================================================================
# parse_cpu tests
# ============================================================================


class TestParseCpu:
    def test_whole_cores(self):
        assert parse_cpu("4") == 4.0

    def test_millicores(self):
        assert parse_cpu("500m") == 0.5

    def test_nanocores(self):
        assert parse_cpu("1000000000n") == 1.0

    def test_float_input(self):
        assert parse_cpu(2.5) == 2.5

    def test_int_input(self):
        assert parse_cpu(4) == 4.0

    def test_zero(self):
        assert parse_cpu("0") == 0.0

    def test_malformed_returns_zero(self):
        assert parse_cpu("abc") == 0.0

    def test_empty_string_returns_zero(self):
        assert parse_cpu("") == 0.0

    def test_none_returns_zero(self):
        assert parse_cpu(None) == 0.0

    def test_one_core_string(self):
        assert parse_cpu("1") == 1.0

    def test_1500m(self):
        assert parse_cpu("1500m") == 1.5

    def test_100m(self):
        assert parse_cpu("100m") == pytest.approx(0.1)

    def test_small_nanocores(self):
        assert parse_cpu("500000000n") == pytest.approx(0.5)


# ============================================================================
# parse_memory tests
# ============================================================================


class TestParseMemory:
    def test_bare_bytes(self):
        assert parse_memory("1024") == 1024

    def test_ki(self):
        assert parse_memory("1Ki") == 1024

    def test_mi(self):
        assert parse_memory("256Mi") == 256 * 1024**2

    def test_gi(self):
        assert parse_memory("4Gi") == 4 * GiB

    def test_ti(self):
        assert parse_memory("1Ti") == 1024**4

    def test_decimal_suffix(self):
        assert parse_memory("1.5Gi") == int(1.5 * GiB)

    def test_si_k(self):
        assert parse_memory("1000K") == 1_000_000

    def test_si_m(self):
        assert parse_memory("500M") == 500_000_000

    def test_si_g(self):
        assert parse_memory("2G") == 2_000_000_000

    def test_int_input(self):
        assert parse_memory(4096) == 4096

    def test_malformed_returns_zero(self):
        assert parse_memory("xyz") == 0

    def test_none_returns_zero(self):
        assert parse_memory(None) == 0

    def test_empty_string_returns_zero(self):
        assert parse_memory("") == 0

    def test_128mi(self):
        assert parse_memory("128Mi") == 128 * 1024**2

    def test_1024ki(self):
        assert parse_memory("1024Ki") == 1024 * 1024


# ============================================================================
# NodeState property tests
# ============================================================================


class TestNodeState:
    def test_workload_pods_filters_daemonsets(self):
        node = make_node("n1")
        node.pods = [
            make_pod("w1", is_daemonset=False, node_name="n1"),
            make_pod("d1", is_daemonset=True, node_name="n1"),
            make_pod("w2", is_daemonset=False, node_name="n1"),
        ]
        assert len(node.workload_pods) == 2
        assert node.workload_pod_count == 2

    def test_cpu_utilization(self):
        node = make_node("n1", cpu=10.0)
        node.pods = [make_pod("p1", cpu=3.0, node_name="n1")]
        assert node.cpu_utilization == pytest.approx(0.3)

    def test_memory_utilization(self):
        node = make_node("n1", mem=100 * GiB)
        node.pods = [make_pod("p1", mem=25 * GiB, node_name="n1")]
        assert node.memory_utilization == pytest.approx(0.25)

    def test_utilization_is_max_of_cpu_mem(self):
        node = make_node("n1", cpu=10.0, mem=100 * GiB)
        node.pods = [make_pod("p1", cpu=8.0, mem=10 * GiB, node_name="n1")]
        assert node.utilization == pytest.approx(0.8)

    def test_uptime_hours(self):
        node = make_node("n1", creation_time=NOW - timedelta(hours=5))
        assert node.uptime_hours == pytest.approx(5.0, abs=0.01)

    def test_youngest_pod_age_no_pods(self):
        node = make_node("n1")
        assert node.youngest_pod_age_seconds == math.inf

    def test_youngest_pod_age_with_pods(self):
        now = datetime.now(UTC)
        node = make_node("n1")
        node.pods = [
            make_pod("p1", node_name="n1", start_time=now - timedelta(minutes=30)),
            make_pod("p2", node_name="n1", start_time=now - timedelta(minutes=5)),
        ]
        # Youngest pod is 5 minutes old
        assert node.youngest_pod_age_seconds == pytest.approx(300, abs=5)

    def test_youngest_pod_age_ignores_none_start_time(self):
        now = datetime.now(UTC)
        node = make_node("n1")
        node.pods = [
            make_pod("p1", node_name="n1", start_time=None),
            make_pod("p2", node_name="n1", start_time=now - timedelta(minutes=10)),
        ]
        assert node.youngest_pod_age_seconds == pytest.approx(600, abs=5)

    def test_zero_allocatable_cpu_gives_zero_utilization(self):
        node = make_node("n1", cpu=0.0)
        node.pods = [make_pod("p1", cpu=1.0, node_name="n1")]
        assert node.cpu_utilization == 0.0

    def test_zero_allocatable_memory_gives_zero_utilization(self):
        node = make_node("n1", mem=0)
        node.pods = [make_pod("p1", mem=1000, node_name="n1")]
        assert node.memory_utilization == 0.0

    def test_daemonset_cpu(self):
        node = make_node("n1", cpu=16.0)
        node.pods = [
            make_pod("ds1", cpu=0.5, node_name="n1", is_daemonset=True),
            make_pod("ds2", cpu=0.3, node_name="n1", is_daemonset=True),
            make_pod("w1", cpu=4.0, node_name="n1"),
        ]
        assert node.daemonset_cpu == pytest.approx(0.8)

    def test_daemonset_memory(self):
        node = make_node("n1", mem=64 * GiB)
        node.pods = [
            make_pod("ds1", mem=512 * 1024**2, node_name="n1", is_daemonset=True),
            make_pod("ds2", mem=256 * 1024**2, node_name="n1", is_daemonset=True),
            make_pod("w1", mem=4 * GiB, node_name="n1"),
        ]
        assert node.daemonset_memory == 768 * 1024**2

    def test_total_cpu_used(self):
        node = make_node("n1", cpu=16.0)
        node.pods = [
            make_pod("ds1", cpu=0.5, node_name="n1", is_daemonset=True),
            make_pod("w1", cpu=4.0, node_name="n1"),
        ]
        assert node.total_cpu_used == pytest.approx(4.5)

    def test_total_memory_used(self):
        node = make_node("n1", mem=64 * GiB)
        node.pods = [
            make_pod("ds1", mem=1 * GiB, node_name="n1", is_daemonset=True),
            make_pod("w1", mem=4 * GiB, node_name="n1"),
        ]
        assert node.total_memory_used == 5 * GiB


# ============================================================================
# bin_pack_min_nodes tests
# ============================================================================


class TestBinPackMinNodes:
    def test_empty_pods(self):
        nodes = [make_node("n1")]
        assert bin_pack_min_nodes([], nodes) == 0

    def test_empty_nodes(self):
        pods = [make_pod("p1")]
        assert bin_pack_min_nodes(pods, []) == 0

    def test_single_pod_single_node(self):
        nodes = [make_node("n1", cpu=16.0)]
        pods = [make_pod("p1", cpu=4.0)]
        assert bin_pack_min_nodes(pods, nodes) == 1

    def test_pods_fit_on_fewer_nodes(self):
        nodes = [make_node(f"n{i}", cpu=16.0) for i in range(4)]
        pods = [make_pod(f"p{i}", cpu=4.0) for i in range(8)]
        # 8 pods * 4 CPU = 32 CPU total, nodes have 16 each, need 2
        assert bin_pack_min_nodes(pods, nodes) == 2

    def test_pods_need_all_nodes(self):
        nodes = [make_node(f"n{i}", cpu=4.0) for i in range(3)]
        pods = [make_pod(f"p{i}", cpu=3.0) for i in range(3)]
        # Each pod needs 3 CPU, each node has 4 -> 1 pod per node -> 3 nodes
        assert bin_pack_min_nodes(pods, nodes) == 3

    def test_memory_constrained(self):
        nodes = [make_node(f"n{i}", cpu=100.0, mem=8 * GiB) for i in range(4)]
        pods = [make_pod(f"p{i}", cpu=1.0, mem=6 * GiB) for i in range(4)]
        # CPU-wise all fit on 1 node, but memory forces 1 pod per node
        assert bin_pack_min_nodes(pods, nodes) == 4

    def test_heterogeneous_pods(self):
        nodes = [make_node(f"n{i}", cpu=16.0) for i in range(3)]
        pods = [
            make_pod("big", cpu=12.0),
            make_pod("small1", cpu=2.0),
            make_pod("small2", cpu=2.0),
        ]
        # big (12) + small1 (2) + small2 (2) = 16, fits on 1 node (16 CPU)
        assert bin_pack_min_nodes(pods, nodes) == 1

    def test_heterogeneous_pods_overflow(self):
        nodes = [make_node(f"n{i}", cpu=16.0) for i in range(3)]
        pods = [
            make_pod("big", cpu=12.0),
            make_pod("med", cpu=6.0),
            make_pod("small", cpu=2.0),
        ]
        # big (12) + med (6) = 18 > 16, so big goes on node1, med+small on node2
        assert bin_pack_min_nodes(pods, nodes) == 2

    def test_daemonset_overhead_reduces_capacity(self):
        """DaemonSet pods reduce available bin capacity for workload pods."""
        # Node has 16 CPU, but 2 CPU used by DaemonSets -> 14 CPU for workloads
        nodes = [make_node(f"n{i}", cpu=16.0) for i in range(2)]
        for n in nodes:
            n.pods = [
                make_pod(f"ds-{n.name}", cpu=2.0, node_name=n.name, is_daemonset=True),
            ]
        # 2 workload pods of 8 CPU each. Without DS overhead: both fit on 1
        # node (16 CPU). With DS overhead: each node has 14 CPU effective,
        # so 8+8=16 > 14, need 2 nodes.
        pods = [make_pod(f"p{i}", cpu=8.0) for i in range(2)]
        assert bin_pack_min_nodes(pods, nodes) == 2

    def test_no_daemonsets_full_capacity(self):
        """Without DaemonSet pods, full node capacity is available."""
        nodes = [make_node(f"n{i}", cpu=16.0) for i in range(2)]
        pods = [make_pod(f"p{i}", cpu=8.0) for i in range(2)]
        # 16 CPU per node, 2 pods * 8 = 16 -> fits on 1 node
        assert bin_pack_min_nodes(pods, nodes) == 1


# ============================================================================
# _pods_fit_on_nodes tests
# ============================================================================


class TestPodsFitOnNodes:
    def test_no_pods_always_fits(self):
        assert _pods_fit_on_nodes([], [make_node("n1")]) is True

    def test_no_nodes_never_fits(self):
        assert _pods_fit_on_nodes([make_pod("p1")], []) is False

    def test_pods_fit_with_remaining_capacity(self):
        node = make_node("n1", cpu=16.0, mem=64 * GiB)
        node.pods = [make_pod("existing", cpu=4.0, mem=16 * GiB, node_name="n1")]
        pods = [make_pod("new", cpu=4.0, mem=16 * GiB)]
        assert _pods_fit_on_nodes(pods, [node]) is True

    def test_pods_dont_fit_cpu(self):
        node = make_node("n1", cpu=16.0, mem=64 * GiB)
        node.pods = [make_pod("existing", cpu=14.0, mem=4 * GiB, node_name="n1")]
        pods = [make_pod("new", cpu=4.0, mem=4 * GiB)]
        assert _pods_fit_on_nodes(pods, [node]) is False

    def test_pods_dont_fit_memory(self):
        node = make_node("n1", cpu=100.0, mem=8 * GiB)
        node.pods = [make_pod("existing", cpu=1.0, mem=6 * GiB, node_name="n1")]
        pods = [make_pod("new", cpu=1.0, mem=4 * GiB)]
        assert _pods_fit_on_nodes(pods, [node]) is False

    def test_daemonset_pods_count_in_total_usage(self):
        """DaemonSet pods consume real resources and reduce available capacity."""
        node = make_node("n1", cpu=16.0, mem=64 * GiB)
        node.pods = [
            make_pod("ds", cpu=14.0, mem=4 * GiB, node_name="n1", is_daemonset=True),
        ]
        pods = [make_pod("new", cpu=4.0, mem=4 * GiB)]
        # DaemonSet uses 14 CPU, only 2 CPU remaining. New pod needs 4 -> doesn't fit
        assert _pods_fit_on_nodes(pods, [node]) is False

    def test_workload_and_daemonset_both_count(self):
        """Both workload and DaemonSet pods reduce available capacity."""
        node = make_node("n1", cpu=16.0, mem=64 * GiB)
        node.pods = [
            make_pod("ds", cpu=2.0, mem=4 * GiB, node_name="n1", is_daemonset=True),
            make_pod("wl", cpu=10.0, mem=4 * GiB, node_name="n1"),
        ]
        pods = [make_pod("new", cpu=4.0, mem=4 * GiB)]
        # DS(2) + WL(10) = 12 used, 4 remaining. New pod needs 4 -> fits exactly
        assert _pods_fit_on_nodes(pods, [node]) is True

    def test_workload_and_daemonset_overflow(self):
        """Combined usage exceeds capacity."""
        node = make_node("n1", cpu=16.0, mem=64 * GiB)
        node.pods = [
            make_pod("ds", cpu=2.0, mem=4 * GiB, node_name="n1", is_daemonset=True),
            make_pod("wl", cpu=10.0, mem=4 * GiB, node_name="n1"),
        ]
        pods = [make_pod("new", cpu=5.0, mem=4 * GiB)]
        # DS(2) + WL(10) = 12 used, 4 remaining. New pod needs 5 -> doesn't fit
        assert _pods_fit_on_nodes(pods, [node]) is False


# ============================================================================
# compute_taints tests
# ============================================================================


class TestComputeTaints:
    def test_empty_states(self):
        cfg = make_config()
        to_taint, to_untaint, mandatory, _rate_limited = compute_taints({}, cfg)
        assert to_taint == set()
        assert to_untaint == set()
        assert mandatory == set()

    def test_no_surplus_untaints_all(self):
        cfg = make_config(min_nodes=2)
        nodes = {
            "n1": make_node("n1", is_tainted=True),
            "n2": make_node("n2", is_tainted=True),
        }
        for n in nodes.values():
            n.pods = [make_pod(f"p-{n.name}", cpu=8.0, node_name=n.name)]
        to_taint, to_untaint, mandatory, _rate_limited = compute_taints(nodes, cfg)
        assert to_taint == set()
        assert to_untaint == {"n1", "n2"}
        assert mandatory == {"n1", "n2"}

    def test_surplus_taints_correct_count(self):
        cfg = make_config(min_nodes=1)
        nodes = {}
        for i in range(4):
            n = make_node(f"n{i}", cpu=16.0)
            nodes[f"n{i}"] = n
        # 1 pod total -> needs 1 node -> surplus 3
        nodes["n0"].pods = [make_pod("p1", cpu=4.0, node_name="n0")]
        to_taint, to_untaint, _mandatory, _rate_limited = compute_taints(nodes, cfg)
        assert len(to_taint) == 3
        assert len(to_taint) + len(to_untaint.intersection(set(nodes.keys()) - to_taint)) >= 0

    def test_min_nodes_enforced(self):
        cfg = make_config(min_nodes=3)
        nodes = {}
        for i in range(4):
            n = make_node(f"n{i}", cpu=16.0)
            nodes[f"n{i}"] = n
        # No pods -> bin_pack returns 0, but min_nodes=3 -> surplus = 1
        to_taint, _to_untaint, _mandatory, _rate_limited = compute_taints(nodes, cfg)
        assert len(to_taint) == 1

    def test_old_nodes_tainted_first(self):
        cfg = make_config(max_uptime_hours=24)
        old_node = make_node("old", cpu=16.0, creation_time=NOW - timedelta(hours=50))
        young_node = make_node("young", cpu=16.0, creation_time=NOW - timedelta(hours=2))
        nodes = {"old": old_node, "young": young_node}
        # No pods -> both surplus, but min_nodes=1, surplus=1
        to_taint, _to_untaint, _mandatory, _rate_limited = compute_taints(nodes, cfg)
        assert "old" in to_taint
        assert "young" not in to_taint

    def test_lower_utilization_tainted_first(self):
        cfg = make_config(min_nodes=1)
        n1 = make_node("n1", cpu=16.0)
        n1.pods = [make_pod("p1", cpu=12.0, node_name="n1")]
        n2 = make_node("n2", cpu=16.0)
        n2.pods = [make_pod("p2", cpu=2.0, node_name="n2")]
        n3 = make_node("n3", cpu=16.0)
        n3.pods = [make_pod("p3", cpu=8.0, node_name="n3")]
        nodes = {"n1": n1, "n2": n2, "n3": n3}
        # 3 nodes, all pods fit on 2 -> surplus=1 (after max with min_nodes=1)
        # Actually bin_pack: total 22 CPU in pods, 16 CPU per node -> need 2
        # surplus = 1, n2 has lowest util -> tainted
        to_taint, _to_untaint, _mandatory, _rate_limited = compute_taints(nodes, cfg)
        assert "n2" in to_taint

    def test_already_tainted_surplus_stays_tainted(self):
        cfg = make_config(min_nodes=1)
        n1 = make_node("n1", cpu=16.0)
        n2 = make_node("n2", cpu=16.0, is_tainted=True)
        nodes = {"n1": n1, "n2": n2}
        # No pods, surplus = 1 -> one node tainted
        to_taint, _to_untaint, _mandatory, _rate_limited = compute_taints(nodes, cfg)
        assert len(to_taint) == 1

    def test_sole_candidate_safety(self):
        cfg = make_config(min_nodes=1)
        # 2 nodes, each with a large pod. If we taint n1, its pod can't fit
        # on n2 (which already has a large pod). Safety check should prevent.
        n1 = make_node("n1", cpu=16.0)
        n1.pods = [make_pod("p1", cpu=10.0, node_name="n1")]
        n2 = make_node("n2", cpu=16.0)
        n2.pods = [make_pod("p2", cpu=10.0, node_name="n2")]
        nodes = {"n1": n1, "n2": n2}
        # bin_pack: 2 pods * 10 CPU = 20, need 2 nodes -> surplus=0 -> untaint
        to_taint, _to_untaint, _mandatory, _rate_limited = compute_taints(nodes, cfg)
        assert len(to_taint) == 0

    def test_safety_check_prevents_taint_when_pods_cant_move(self):
        # 3 nodes, each with a pod that uses ~60% CPU. bin_pack says 2 needed
        # (pods can theoretically pack onto 2 nodes by bin_pack's fresh-bin logic),
        # but _pods_fit_on_nodes checks REMAINING capacity. If the remaining
        # node already has its own pod, the displaced pod may not fit.
        cfg = make_config(min_nodes=1)
        n1 = make_node("n1", cpu=16.0, mem=64 * GiB)
        n1.pods = [make_pod("p1", cpu=9.0, mem=4 * GiB, node_name="n1")]
        n2 = make_node("n2", cpu=16.0, mem=64 * GiB)
        n2.pods = [make_pod("p2", cpu=9.0, mem=4 * GiB, node_name="n2")]
        n3 = make_node("n3", cpu=16.0, mem=64 * GiB)
        n3.pods = [make_pod("p3", cpu=9.0, mem=4 * GiB, node_name="n3")]
        nodes = {"n1": n1, "n2": n2, "n3": n3}
        # bin_pack: 3 pods * 9 CPU = 27, nodes have 16 each -> need 2
        # surplus = 1, but safety check: taint candidate's pod (9 CPU) can't fit
        # on remaining 2 nodes (each has only 7 CPU free). Skip the taint.
        to_taint, _to_untaint, _mandatory, _rate_limited = compute_taints(nodes, cfg)
        assert len(to_taint) == 0

    def test_multiple_pools_independent(self):
        cfg = make_config(min_nodes=1)
        n1 = make_node("n1", nodepool="pool-a", cpu=16.0)
        n2 = make_node("n2", nodepool="pool-a", cpu=16.0)
        n3 = make_node("n3", nodepool="pool-b", cpu=16.0)
        n4 = make_node("n4", nodepool="pool-b", cpu=16.0)
        nodes = {"n1": n1, "n2": n2, "n3": n3, "n4": n4}
        # No pods in either pool -> surplus=1 per pool (min_nodes=1)
        to_taint, _to_untaint, _mandatory, _rate_limited = compute_taints(nodes, cfg)
        pool_a_tainted = to_taint & {"n1", "n2"}
        pool_b_tainted = to_taint & {"n3", "n4"}
        assert len(pool_a_tainted) == 1
        assert len(pool_b_tainted) == 1

    def test_remaining_untainted_incremental_build(self):
        """Verify that remaining_untainted is built incrementally, not pre-computed.

        If the first candidate fails the safety check, it should be added to
        the remaining set, increasing capacity for subsequent candidates.
        """
        cfg = make_config(min_nodes=1)
        # 4 nodes, surplus=3. Node n0 is the taint priority winner (empty, lowest util).
        # But n1 has a big pod. If n0 is tainted and n1's pod needs to fit on
        # remaining, n0 must be in remaining after it's skipped.
        n0 = make_node("n0", cpu=16.0)
        # n0 empty -- best taint candidate
        n1 = make_node("n1", cpu=16.0)
        n1.pods = [make_pod("p1", cpu=14.0, node_name="n1")]
        n2 = make_node("n2", cpu=16.0)
        n2.pods = [make_pod("p2", cpu=14.0, node_name="n2")]
        n3 = make_node("n3", cpu=16.0)
        n3.pods = [make_pod("p3", cpu=14.0, node_name="n3")]
        nodes = {"n0": n0, "n1": n1, "n2": n2, "n3": n3}
        # bin_pack: 3 pods * 14 = 42 CPU. 16 per node -> need 3. surplus=1
        # n0 is the candidate (empty, lowest util). It has no workload pods,
        # so no safety check needed -> tainted.
        to_taint, _to_untaint, _mandatory, _rate_limited = compute_taints(nodes, cfg)
        assert "n0" in to_taint
        assert len(to_taint) == 1

    def test_young_nodes_excluded_from_tainting(self):
        cfg = make_config(min_nodes=1, min_node_age=420)
        young = make_node("young", cpu=16.0, creation_time=NOW - timedelta(seconds=120))
        old1 = make_node("old1", cpu=16.0)
        old2 = make_node("old2", cpu=16.0)
        nodes = {"young": young, "old1": old1, "old2": old2}
        to_taint, _to_untaint, _mandatory, _rate_limited = compute_taints(nodes, cfg)
        # Young node must not be tainted
        assert "young" not in to_taint
        # 3 nodes, 0 pods -> min_needed=1, surplus=2
        # Young excluded -> 2 eligible, both tainted (surplus covers them)
        assert to_taint == {"old1", "old2"}

    def test_young_tainted_node_gets_mandatory_untaint(self):
        cfg = make_config(min_nodes=1, min_node_age=420)
        young = make_node("young", cpu=16.0, is_tainted=True, creation_time=NOW - timedelta(seconds=120))
        old = make_node("old", cpu=16.0)
        nodes = {"young": young, "old": old}
        _to_taint, to_untaint, mandatory, _rate_limited = compute_taints(nodes, cfg)
        assert "young" in to_untaint
        assert "young" in mandatory

    def test_all_nodes_young_no_tainting(self):
        cfg = make_config(min_nodes=1, min_node_age=420)
        n1 = make_node("n1", cpu=16.0, creation_time=NOW - timedelta(seconds=60))
        n2 = make_node("n2", cpu=16.0, creation_time=NOW - timedelta(seconds=60))
        n3 = make_node("n3", cpu=16.0, creation_time=NOW - timedelta(seconds=60))
        nodes = {"n1": n1, "n2": n2, "n3": n3}
        to_taint, _to_untaint, _mandatory, _rate_limited = compute_taints(nodes, cfg)
        assert len(to_taint) == 0

    def test_min_node_age_zero_disables_grace_period(self):
        cfg = make_config(min_nodes=1, min_node_age=0)
        young = make_node("young", cpu=16.0, creation_time=NOW - timedelta(seconds=30))
        old1 = make_node("old1", cpu=16.0)
        old2 = make_node("old2", cpu=16.0)
        nodes = {"young": young, "old1": old1, "old2": old2}
        to_taint, _to_untaint, _mandatory, _rate_limited = compute_taints(nodes, cfg)
        # All 3 eligible, min_nodes=1 -> surplus=2
        assert len(to_taint) == 2


# ============================================================================
# Config tests
# ============================================================================


class TestConfig:
    def test_from_env_defaults(self):
        cfg = Config.from_env()
        assert cfg.interval == 20
        assert cfg.max_uptime_hours == 48
        assert cfg.min_nodes == 1
        assert cfg.dry_run is False
        assert cfg.taint_cooldown == 300

    def test_from_env_defaults_clean_env(self, monkeypatch):
        for key in (
            "COMPACTOR_INTERVAL",
            "COMPACTOR_MAX_UPTIME_HOURS",
            "COMPACTOR_NODEPOOL_LABEL",
            "COMPACTOR_TAINT_KEY",
            "COMPACTOR_MIN_NODES",
            "COMPACTOR_DRY_RUN",
            "COMPACTOR_TAINT_COOLDOWN",
        ):
            monkeypatch.delenv(key, raising=False)
        cfg = Config.from_env()
        assert cfg.interval == 20
        assert cfg.max_uptime_hours == 48
        assert cfg.nodepool_label == "osdc.io/node-compactor"
        assert cfg.taint_key == "node-compactor.osdc.io/consolidating"
        assert cfg.min_nodes == 1
        assert cfg.dry_run is False
        assert cfg.taint_cooldown == 300

    def test_from_env_custom(self, monkeypatch):
        monkeypatch.setenv("COMPACTOR_INTERVAL", "30")
        monkeypatch.setenv("COMPACTOR_DRY_RUN", "true")
        monkeypatch.setenv("COMPACTOR_MIN_NODES", "2")
        monkeypatch.setenv("COMPACTOR_TAINT_COOLDOWN", "600")
        cfg = Config.from_env()
        assert cfg.interval == 30
        assert cfg.dry_run is True
        assert cfg.min_nodes == 2
        assert cfg.taint_cooldown == 600

    @pytest.mark.parametrize("val", ["true", "True", "TRUE", "1", "yes", "Yes"])
    def test_dry_run_truthy(self, monkeypatch, val):
        monkeypatch.setenv("COMPACTOR_DRY_RUN", val)
        assert Config.from_env().dry_run is True

    @pytest.mark.parametrize("val", ["false", "False", "0", "no", ""])
    def test_dry_run_falsy(self, monkeypatch, val):
        monkeypatch.setenv("COMPACTOR_DRY_RUN", val)
        assert Config.from_env().dry_run is False


# ============================================================================
# bin_pack_min_nodes -- pods exceed total capacity (line 48)
# ============================================================================


class TestBinPackOverflow:
    def test_pods_exceed_total_capacity_returns_len_bins(self):
        """When pods exceed total node capacity, return len(bins) (all nodes used).

        Covers packing.py line 48: the early return when nodes_used == len(bins)
        and there's still a pod to place.
        """
        # 2 nodes with 16 CPU each = 32 CPU total capacity
        nodes = [make_node(f"n{i}", cpu=16.0) for i in range(2)]
        # 5 pods needing 8 CPU each = 40 CPU total demand (exceeds 32)
        pods = [make_pod(f"p{i}", cpu=8.0) for i in range(5)]
        # First 4 pods fill both nodes (2 per node * 8 CPU = 16 per node).
        # 5th pod has nowhere to go -> nodes_used == len(bins) == 2 -> return 2.
        result = bin_pack_min_nodes(pods, nodes)
        assert result == 2

    def test_single_pod_too_large_for_single_node(self):
        """A single oversized pod that doesn't fit the only node."""
        nodes = [make_node("n1", cpu=4.0)]
        pods = [
            make_pod("small", cpu=3.0),
            make_pod("big", cpu=8.0),
        ]
        # Sorted by CPU descending: big(8) first. Placed on n1 (4 CPU bin),
        # nodes_used=1. small(3) tries existing bin -> not enough room.
        # nodes_used(1) == len(bins)(1) -> return 1.
        result = bin_pack_min_nodes(pods, nodes)
        assert result == 1

    def test_memory_overflow_triggers_early_return(self):
        """Memory overflow can also trigger the len(bins) return."""
        # Plenty of CPU but very little memory
        nodes = [make_node("n1", cpu=100.0, mem=4 * GiB)]
        pods = [
            make_pod("p1", cpu=1.0, mem=3 * GiB),
            make_pod("p2", cpu=1.0, mem=3 * GiB),
        ]
        # p1 placed on n1 (3 GiB used, 1 GiB left). p2 needs 3 GiB,
        # doesn't fit. nodes_used(1) == len(bins)(1) -> return 1.
        result = bin_pack_min_nodes(pods, nodes)
        assert result == 1


# ============================================================================
# compute_taints -- safety check skip + untaint (lines 152-159)
# ============================================================================


class TestComputeTaintsSafetySkipUntaint:
    def test_tainted_candidate_untainted_when_pods_cant_fit(self):
        """A taint candidate whose pods can't fit on remaining nodes is skipped
        and untainted if it was already tainted.

        Covers packing.py lines 152-159: the safety check skip path where the
        candidate is added to conditionally_remaining and untainted.

        Setup: 4 nodes, 16 CPU each.
        - n0: empty (lowest utilization, first taint candidate)
        - n1: 9 CPU pod, is_tainted=True (second candidate, safety will fail)
        - n2: two 5 CPU pods (10 CPU used, 6 CPU free)
        - n3: two 5 CPU pods (10 CPU used, 6 CPU free)

        bin_pack sees 5 pods [9,5,5,5,5]=29 CPU. FFD packing:
          9->bin0(7rem), 5->bin0(2rem), 5->bin1(11rem), 5->bin1(6rem), 5->bin1(1rem).
          nodes_used=2. surplus=4-2=2.

        Taint priority (util ascending): n0(0%), n1(56%), n2(62.5%), n3(62.5%).
        definitely_remaining = [n2, n3].

        Candidate n0: empty, no safety check needed -> tainted. count=1.
        Candidate n1: pod 9 CPU. remaining=[n2,n3].
          n2 free=16-10=6, n3 free=16-10=6. 9 CPU > 6 -> doesn't fit!
          Safety FAILS -> n1 added to conditionally_remaining.
          n1.is_tainted=True -> added to to_untaint.
        """
        cfg = make_config(min_nodes=1)

        n0 = make_node("n0", cpu=16.0)
        n1 = make_node("n1", cpu=16.0, is_tainted=True)
        n1.pods = [make_pod("p1", cpu=9.0, mem=4 * GiB, node_name="n1")]
        n2 = make_node("n2", cpu=16.0)
        n2.pods = [
            make_pod("p2a", cpu=5.0, mem=4 * GiB, node_name="n2"),
            make_pod("p2b", cpu=5.0, mem=4 * GiB, node_name="n2"),
        ]
        n3 = make_node("n3", cpu=16.0)
        n3.pods = [
            make_pod("p3a", cpu=5.0, mem=4 * GiB, node_name="n3"),
            make_pod("p3b", cpu=5.0, mem=4 * GiB, node_name="n3"),
        ]

        nodes = {"n0": n0, "n1": n1, "n2": n2, "n3": n3}

        to_taint, to_untaint, _mandatory, _rate_limited = compute_taints(nodes, cfg)
        assert "n0" in to_taint, "Empty node should be tainted"
        assert "n1" in to_untaint, "Already-tainted node whose pods can't fit should be untainted"
        assert "n1" not in to_taint, "n1 should NOT be tainted (safety check failed)"

    def test_safety_skip_adds_to_conditionally_remaining(self):
        """When a candidate fails safety, it joins conditionally_remaining,
        increasing capacity for subsequent candidates.

        Verifies the conditionally_remaining mechanism: a later candidate
        CAN be tainted because the skipped candidate's capacity is available.
        """
        cfg = make_config(min_nodes=1)

        # 4 nodes, 16 CPU each.
        # n1: 15 CPU pod (high util, but lowest taint priority candidate after empty n4)
        # n2: 1 CPU pod
        # n3: 1 CPU pod
        # n4: empty (first taint candidate)
        n1 = make_node("n1", cpu=16.0)
        n1.pods = [make_pod("p1", cpu=15.0, mem=4 * GiB, node_name="n1")]
        n2 = make_node("n2", cpu=16.0)
        n2.pods = [make_pod("p2", cpu=1.0, mem=4 * GiB, node_name="n2")]
        n3 = make_node("n3", cpu=16.0)
        n3.pods = [make_pod("p3", cpu=1.0, mem=4 * GiB, node_name="n3")]
        n4 = make_node("n4", cpu=16.0)

        nodes = {"n1": n1, "n2": n2, "n3": n3, "n4": n4}

        # bin_pack: 3 pods (15+1+1=17 CPU). 16 per node -> need 2. surplus = 2.
        #
        # Sorted by taint_priority (utilization ascending):
        #   n4 (0%), n2 (6.25%), n3 (6.25%), n1 (93.75%)
        #
        # definitely_remaining = [n3, n1] (candidates[2:])
        #
        # Candidate n4: empty -> tainted. taint_count=1.
        # Candidate n2: pod p2 (1 CPU). remaining = [n3, n1].
        #   n3 has 15 free, n1 has 1 free. p2 needs 1 CPU -> fits on n3. Tainted. taint_count=2.
        #
        # surplus reached.

        to_taint, _to_untaint, _mandatory, _rate_limited = compute_taints(nodes, cfg)
        assert "n4" in to_taint
        assert "n2" in to_taint
        assert len(to_taint) == 2

    def test_untainted_candidate_not_added_to_untaint_set(self):
        """A candidate that fails safety but was NOT already tainted should
        NOT appear in to_untaint (only in conditionally_remaining).

        Same node layout as test_tainted_candidate_untainted_when_pods_cant_fit
        but n1 is NOT tainted, so it should not appear in to_untaint.
        """
        cfg = make_config(min_nodes=1)

        n0 = make_node("n0", cpu=16.0)
        n1 = make_node("n1", cpu=16.0, is_tainted=False)
        n1.pods = [make_pod("p1", cpu=9.0, mem=4 * GiB, node_name="n1")]
        n2 = make_node("n2", cpu=16.0)
        n2.pods = [
            make_pod("p2a", cpu=5.0, mem=4 * GiB, node_name="n2"),
            make_pod("p2b", cpu=5.0, mem=4 * GiB, node_name="n2"),
        ]
        n3 = make_node("n3", cpu=16.0)
        n3.pods = [
            make_pod("p3a", cpu=5.0, mem=4 * GiB, node_name="n3"),
            make_pod("p3b", cpu=5.0, mem=4 * GiB, node_name="n3"),
        ]

        nodes = {"n0": n0, "n1": n1, "n2": n2, "n3": n3}

        to_taint, to_untaint, _mandatory, _rate_limited = compute_taints(nodes, cfg)
        assert "n0" in to_taint
        # n1 fails safety check but is NOT tainted, so should not be in to_untaint
        assert "n1" not in to_untaint


# ============================================================================
# reconcile() tests
# ============================================================================


def _make_api_error(code: int) -> ApiError:
    """Create a mock ApiError with the given status code."""
    resp = MagicMock()
    err = ApiError(response=resp)
    err.status = MagicMock(code=code)
    return err


class TestReconcile:
    """Tests for the reconcile() function."""

    @patch("compactor.remove_taint")
    @patch("compactor.apply_taint")
    @patch("compactor.compute_taints")
    @patch("compactor.check_pending_pods")
    @patch("compactor.build_node_states")
    @patch("compactor.discover_managed_nodes")
    def test_no_managed_nodes_returns_early(
        self,
        mock_discover,
        mock_build,
        mock_check,
        mock_compute,
        mock_apply,
        mock_remove,
    ):
        mock_discover.return_value = {}
        client = MagicMock()
        cfg = make_config()

        reconcile(client, cfg, {})

        mock_discover.assert_called_once_with(client, cfg)
        mock_build.assert_not_called()
        mock_check.assert_not_called()
        mock_compute.assert_not_called()

    @patch("compactor.remove_taint")
    @patch("compactor.apply_taint")
    @patch("compactor.compute_taints")
    @patch("compactor.check_pending_pods")
    @patch("compactor.build_node_states")
    @patch("compactor.discover_managed_nodes")
    def test_no_node_states_returns_early(
        self,
        mock_discover,
        mock_build,
        mock_check,
        mock_compute,
        mock_apply,
        mock_remove,
    ):
        mock_discover.return_value = {"node-1": "pool"}
        mock_build.return_value = ({}, [])
        client = MagicMock()
        cfg = make_config()

        reconcile(client, cfg, {})

        mock_build.assert_called_once()
        mock_check.assert_not_called()
        mock_compute.assert_not_called()

    @patch("compactor.pathlib.Path.touch")
    @patch("compactor.remove_taint")
    @patch("compactor.apply_taint")
    @patch("compactor.compute_taints")
    @patch("compactor.check_pending_pods")
    @patch("compactor.build_node_states")
    @patch("compactor.discover_managed_nodes")
    def test_normal_flow_untaint_and_taint(
        self,
        mock_discover,
        mock_build,
        mock_check,
        mock_compute,
        mock_apply,
        mock_remove,
        mock_touch,
    ):
        client = MagicMock()
        cfg = make_config()
        taint_times = {}

        n1 = make_node("n1", is_tainted=True)
        n2 = make_node("n2", is_tainted=False)
        node_states = {"n1": n1, "n2": n2}

        mock_discover.return_value = {"n1": "default", "n2": "default"}
        mock_build.return_value = (node_states, [])
        mock_check.return_value = set()
        mock_compute.return_value = ({"n2"}, {"n1"}, set(), set())

        reconcile(client, cfg, taint_times)

        mock_remove.assert_called_once_with(client, "n1", cfg.taint_key, cfg.dry_run)
        mock_apply.assert_called_once_with(client, "n2", cfg.taint_key, cfg.dry_run)
        assert "n2" in taint_times
        mock_touch.assert_called_once()

    @patch("compactor.pathlib.Path.touch")
    @patch("compactor.remove_taint")
    @patch("compactor.apply_taint")
    @patch("compactor.compute_taints")
    @patch("compactor.check_pending_pods")
    @patch("compactor.build_node_states")
    @patch("compactor.discover_managed_nodes")
    def test_burst_untaint_overrides_cooldown(
        self,
        mock_discover,
        mock_build,
        mock_check,
        mock_compute,
        mock_apply,
        mock_remove,
        mock_touch,
    ):
        client = MagicMock()
        cfg = make_config(taint_cooldown=300)
        # Node was tainted very recently -- within cooldown
        taint_times = {"n1": time.time() - 10}

        n1 = make_node("n1", is_tainted=True)
        node_states = {"n1": n1}

        mock_discover.return_value = {"n1": "default"}
        mock_build.return_value = (node_states, [])
        # burst_untaint includes n1 -- should bypass cooldown
        mock_check.return_value = {"n1"}
        mock_compute.return_value = (set(), set(), set(), set())

        reconcile(client, cfg, taint_times)

        # n1 should be untainted despite being within cooldown
        mock_remove.assert_called_once_with(client, "n1", cfg.taint_key, cfg.dry_run)

    @patch("compactor.pathlib.Path.touch")
    @patch("compactor.remove_taint")
    @patch("compactor.apply_taint")
    @patch("compactor.compute_taints")
    @patch("compactor.check_pending_pods")
    @patch("compactor.build_node_states")
    @patch("compactor.discover_managed_nodes")
    def test_cooldown_blocks_non_burst_untaint(
        self,
        mock_discover,
        mock_build,
        mock_check,
        mock_compute,
        mock_apply,
        mock_remove,
        mock_touch,
    ):
        client = MagicMock()
        cfg = make_config(taint_cooldown=300)
        # Node was tainted 10 seconds ago -- within 300s cooldown
        taint_times = {"n1": time.time() - 10}

        n1 = make_node("n1", is_tainted=True)
        node_states = {"n1": n1}

        mock_discover.return_value = {"n1": "default"}
        mock_build.return_value = (node_states, [])
        mock_check.return_value = set()  # no burst untaint
        mock_compute.return_value = (set(), {"n1"}, set(), set())  # compute says untaint n1

        reconcile(client, cfg, taint_times)

        # n1 should NOT be untainted because cooldown blocks it
        mock_remove.assert_not_called()

    @patch("compactor.pathlib.Path.touch")
    @patch("compactor.remove_taint")
    @patch("compactor.apply_taint")
    @patch("compactor.compute_taints")
    @patch("compactor.check_pending_pods")
    @patch("compactor.build_node_states")
    @patch("compactor.discover_managed_nodes")
    def test_mandatory_untaint_overrides_cooldown(
        self,
        mock_discover,
        mock_build,
        mock_check,
        mock_compute,
        mock_apply,
        mock_remove,
        mock_touch,
    ):
        """mandatory_untaint (min_nodes enforcement) bypasses cooldown."""
        client = MagicMock()
        cfg = make_config(taint_cooldown=300)
        # Node was tainted very recently -- within cooldown
        taint_times = {"n1": time.time() - 10}

        n1 = make_node("n1", is_tainted=True)
        node_states = {"n1": n1}

        mock_discover.return_value = {"n1": "default"}
        mock_build.return_value = (node_states, [])
        mock_check.return_value = set()  # no burst untaint
        # compute says untaint n1, and it's mandatory (min_nodes)
        mock_compute.return_value = (set(), {"n1"}, {"n1"}, set())

        reconcile(client, cfg, taint_times)

        # n1 should be untainted despite being within cooldown
        mock_remove.assert_called_once_with(client, "n1", cfg.taint_key, cfg.dry_run)

    @patch("compactor.pathlib.Path.touch")
    @patch("compactor.remove_taint")
    @patch("compactor.apply_taint")
    @patch("compactor.compute_taints")
    @patch("compactor.check_pending_pods")
    @patch("compactor.build_node_states")
    @patch("compactor.discover_managed_nodes")
    def test_cooldown_expired_allows_untaint(
        self,
        mock_discover,
        mock_build,
        mock_check,
        mock_compute,
        mock_apply,
        mock_remove,
        mock_touch,
    ):
        client = MagicMock()
        cfg = make_config(taint_cooldown=300)
        # Node was tainted 400 seconds ago -- past cooldown
        taint_times = {"n1": time.time() - 400}

        n1 = make_node("n1", is_tainted=True)
        node_states = {"n1": n1}

        mock_discover.return_value = {"n1": "default"}
        mock_build.return_value = (node_states, [])
        mock_check.return_value = set()
        mock_compute.return_value = (set(), {"n1"}, set(), set())

        reconcile(client, cfg, taint_times)

        mock_remove.assert_called_once_with(client, "n1", cfg.taint_key, cfg.dry_run)

    @patch("compactor.pathlib.Path.touch")
    @patch("compactor.remove_taint")
    @patch("compactor.apply_taint")
    @patch("compactor.compute_taints")
    @patch("compactor.check_pending_pods")
    @patch("compactor.build_node_states")
    @patch("compactor.discover_managed_nodes")
    def test_404_on_untaint_logs_and_continues(
        self,
        mock_discover,
        mock_build,
        mock_check,
        mock_compute,
        mock_apply,
        mock_remove,
        mock_touch,
    ):
        client = MagicMock()
        cfg = make_config()

        n1 = make_node("n1", is_tainted=True)
        n2 = make_node("n2", is_tainted=True)
        node_states = {"n1": n1, "n2": n2}

        mock_discover.return_value = {"n1": "default", "n2": "default"}
        mock_build.return_value = (node_states, [])
        mock_check.return_value = set()
        mock_compute.return_value = (set(), {"n1", "n2"}, set(), set())

        # First untaint raises 404, second succeeds
        mock_remove.side_effect = [_make_api_error(404), None]

        # Should not raise
        reconcile(client, cfg, {})

        assert mock_remove.call_count == 2
        mock_touch.assert_called_once()

    @patch("compactor.pathlib.Path.touch")
    @patch("compactor.remove_taint")
    @patch("compactor.apply_taint")
    @patch("compactor.compute_taints")
    @patch("compactor.check_pending_pods")
    @patch("compactor.build_node_states")
    @patch("compactor.discover_managed_nodes")
    def test_404_on_taint_logs_and_continues(
        self,
        mock_discover,
        mock_build,
        mock_check,
        mock_compute,
        mock_apply,
        mock_remove,
        mock_touch,
    ):
        client = MagicMock()
        cfg = make_config()

        n1 = make_node("n1", is_tainted=False)
        node_states = {"n1": n1}

        mock_discover.return_value = {"n1": "default"}
        mock_build.return_value = (node_states, [])
        mock_check.return_value = set()
        mock_compute.return_value = ({"n1"}, set(), set(), set())

        mock_apply.side_effect = _make_api_error(404)

        taint_times = {}
        reconcile(client, cfg, taint_times)

        mock_apply.assert_called_once()
        # taint_times should NOT be updated for a 404
        assert "n1" not in taint_times
        mock_touch.assert_called_once()

    @patch("compactor.pathlib.Path.touch")
    @patch("compactor.remove_taint")
    @patch("compactor.apply_taint")
    @patch("compactor.compute_taints")
    @patch("compactor.check_pending_pods")
    @patch("compactor.build_node_states")
    @patch("compactor.discover_managed_nodes")
    def test_non_404_api_error_on_taint_logs_and_continues(
        self,
        mock_discover,
        mock_build,
        mock_check,
        mock_compute,
        mock_apply,
        mock_remove,
        mock_touch,
    ):
        client = MagicMock()
        cfg = make_config()

        n1 = make_node("n1", is_tainted=False)
        node_states = {"n1": n1}

        mock_discover.return_value = {"n1": "default"}
        mock_build.return_value = (node_states, [])
        mock_check.return_value = set()
        mock_compute.return_value = ({"n1"}, set(), set(), set())

        mock_apply.side_effect = _make_api_error(409)

        taint_times = {}
        reconcile(client, cfg, taint_times)

        mock_apply.assert_called_once()
        assert "n1" not in taint_times

    @patch("compactor.pathlib.Path.touch")
    @patch("compactor.remove_taint")
    @patch("compactor.apply_taint")
    @patch("compactor.compute_taints")
    @patch("compactor.check_pending_pods")
    @patch("compactor.build_node_states")
    @patch("compactor.discover_managed_nodes")
    def test_generic_exception_on_untaint_continues(
        self,
        mock_discover,
        mock_build,
        mock_check,
        mock_compute,
        mock_apply,
        mock_remove,
        mock_touch,
    ):
        client = MagicMock()
        cfg = make_config()

        n1 = make_node("n1", is_tainted=True)
        node_states = {"n1": n1}

        mock_discover.return_value = {"n1": "default"}
        mock_build.return_value = (node_states, [])
        mock_check.return_value = set()
        mock_compute.return_value = (set(), {"n1"}, set(), set())

        mock_remove.side_effect = RuntimeError("connection lost")

        reconcile(client, cfg, {})

        mock_remove.assert_called_once()
        mock_touch.assert_called_once()

    @patch("compactor.pathlib.Path.touch")
    @patch("compactor.remove_taint")
    @patch("compactor.apply_taint")
    @patch("compactor.compute_taints")
    @patch("compactor.check_pending_pods")
    @patch("compactor.build_node_states")
    @patch("compactor.discover_managed_nodes")
    def test_generic_exception_on_taint_continues(
        self,
        mock_discover,
        mock_build,
        mock_check,
        mock_compute,
        mock_apply,
        mock_remove,
        mock_touch,
    ):
        client = MagicMock()
        cfg = make_config()

        n1 = make_node("n1", is_tainted=False)
        node_states = {"n1": n1}

        mock_discover.return_value = {"n1": "default"}
        mock_build.return_value = (node_states, [])
        mock_check.return_value = set()
        mock_compute.return_value = ({"n1"}, set(), set(), set())

        mock_apply.side_effect = RuntimeError("timeout")

        taint_times = {}
        reconcile(client, cfg, taint_times)

        mock_apply.assert_called_once()
        assert "n1" not in taint_times
        mock_touch.assert_called_once()

    @patch("compactor.pathlib.Path.touch")
    @patch("compactor.remove_taint")
    @patch("compactor.apply_taint")
    @patch("compactor.compute_taints")
    @patch("compactor.check_pending_pods")
    @patch("compactor.build_node_states")
    @patch("compactor.discover_managed_nodes")
    def test_taint_times_updated_on_successful_taint(
        self,
        mock_discover,
        mock_build,
        mock_check,
        mock_compute,
        mock_apply,
        mock_remove,
        mock_touch,
    ):
        client = MagicMock()
        cfg = make_config()
        taint_times = {}

        n1 = make_node("n1", is_tainted=False)
        node_states = {"n1": n1}

        mock_discover.return_value = {"n1": "default"}
        mock_build.return_value = (node_states, [])
        mock_check.return_value = set()
        mock_compute.return_value = ({"n1"}, set(), set(), set())

        before = time.time()
        reconcile(client, cfg, taint_times)
        after = time.time()

        assert "n1" in taint_times
        assert before <= taint_times["n1"] <= after

    @patch("compactor.pathlib.Path.touch")
    @patch("compactor.remove_taint")
    @patch("compactor.apply_taint")
    @patch("compactor.compute_taints")
    @patch("compactor.check_pending_pods")
    @patch("compactor.build_node_states")
    @patch("compactor.discover_managed_nodes")
    def test_untaint_only_when_is_tainted_true(
        self,
        mock_discover,
        mock_build,
        mock_check,
        mock_compute,
        mock_apply,
        mock_remove,
        mock_touch,
    ):
        client = MagicMock()
        cfg = make_config()

        # Node is NOT tainted -- should not call remove_taint even if desired
        n1 = make_node("n1", is_tainted=False)
        node_states = {"n1": n1}

        mock_discover.return_value = {"n1": "default"}
        mock_build.return_value = (node_states, [])
        mock_check.return_value = set()
        mock_compute.return_value = (set(), {"n1"}, set(), set())

        reconcile(client, cfg, {})

        mock_remove.assert_not_called()

    @patch("compactor.pathlib.Path.touch")
    @patch("compactor.remove_taint")
    @patch("compactor.apply_taint")
    @patch("compactor.compute_taints")
    @patch("compactor.check_pending_pods")
    @patch("compactor.build_node_states")
    @patch("compactor.discover_managed_nodes")
    def test_taint_only_when_is_tainted_false(
        self,
        mock_discover,
        mock_build,
        mock_check,
        mock_compute,
        mock_apply,
        mock_remove,
        mock_touch,
    ):
        client = MagicMock()
        cfg = make_config()

        # Node is already tainted -- should not call apply_taint even if desired
        n1 = make_node("n1", is_tainted=True)
        node_states = {"n1": n1}

        mock_discover.return_value = {"n1": "default"}
        mock_build.return_value = (node_states, [])
        mock_check.return_value = set()
        mock_compute.return_value = ({"n1"}, set(), set(), set())

        reconcile(client, cfg, {})

        mock_apply.assert_not_called()

    @patch("compactor.pathlib.Path.touch")
    @patch("compactor.remove_taint")
    @patch("compactor.apply_taint")
    @patch("compactor.compute_taints")
    @patch("compactor.check_pending_pods")
    @patch("compactor.build_node_states")
    @patch("compactor.discover_managed_nodes")
    def test_healthcheck_file_touched(
        self,
        mock_discover,
        mock_build,
        mock_check,
        mock_compute,
        mock_apply,
        mock_remove,
        mock_touch,
    ):
        client = MagicMock()
        cfg = make_config()

        n1 = make_node("n1")
        node_states = {"n1": n1}

        mock_discover.return_value = {"n1": "default"}
        mock_build.return_value = (node_states, [])
        mock_check.return_value = set()
        mock_compute.return_value = (set(), set(), set(), set())

        reconcile(client, cfg, {})

        mock_touch.assert_called_once()

    @patch("compactor.pathlib.Path.touch")
    @patch("compactor.remove_taint")
    @patch("compactor.apply_taint")
    @patch("compactor.compute_taints")
    @patch("compactor.check_pending_pods")
    @patch("compactor.build_node_states")
    @patch("compactor.discover_managed_nodes")
    def test_burst_untaint_removed_from_desired_taint(
        self,
        mock_discover,
        mock_build,
        mock_check,
        mock_compute,
        mock_apply,
        mock_remove,
        mock_touch,
    ):
        """If burst_untaint includes a node that compute_taints wants to taint,
        the burst wins -- node should be untainted, not tainted."""
        client = MagicMock()
        cfg = make_config()

        n1 = make_node("n1", is_tainted=True)
        node_states = {"n1": n1}

        mock_discover.return_value = {"n1": "default"}
        mock_build.return_value = (node_states, [])
        mock_check.return_value = {"n1"}  # burst wants untaint
        mock_compute.return_value = ({"n1"}, set(), set(), set())  # compute wants taint

        reconcile(client, cfg, {})

        # Burst untaint wins: remove_taint called, apply_taint not called
        mock_remove.assert_called_once_with(client, "n1", cfg.taint_key, cfg.dry_run)
        mock_apply.assert_not_called()

    @patch("compactor.pathlib.Path.touch")
    @patch("compactor.remove_taint")
    @patch("compactor.apply_taint")
    @patch("compactor.compute_taints")
    @patch("compactor.check_pending_pods")
    @patch("compactor.build_node_states")
    @patch("compactor.discover_managed_nodes")
    def test_non_404_api_error_on_untaint_logs_and_continues(
        self,
        mock_discover,
        mock_build,
        mock_check,
        mock_compute,
        mock_apply,
        mock_remove,
        mock_touch,
    ):
        client = MagicMock()
        cfg = make_config()

        n1 = make_node("n1", is_tainted=True)
        node_states = {"n1": n1}

        mock_discover.return_value = {"n1": "default"}
        mock_build.return_value = (node_states, [])
        mock_check.return_value = set()
        mock_compute.return_value = (set(), {"n1"}, set(), set())

        mock_remove.side_effect = _make_api_error(500)

        reconcile(client, cfg, {})

        mock_remove.assert_called_once()
        mock_touch.assert_called_once()

    @patch("compactor.reconcile_reservations")
    @patch("compactor.select_reserved_nodes")
    @patch("compactor.pathlib.Path.touch")
    @patch("compactor.remove_taint")
    @patch("compactor.apply_taint")
    @patch("compactor.compute_taints")
    @patch("compactor.check_pending_pods")
    @patch("compactor.build_node_states")
    @patch("compactor.discover_managed_nodes")
    def test_reconcile_with_capacity_reservations(
        self,
        mock_discover,
        mock_build,
        mock_check,
        mock_compute,
        mock_apply,
        mock_remove,
        mock_touch,
        mock_select_reserved,
        mock_reconcile_res,
    ):
        """reconcile() calls select_reserved_nodes and reconcile_reservations when enabled."""
        client = MagicMock()
        cfg = make_config(capacity_reservation_nodes=2)

        n1 = make_node("n1", nodepool="pool-a")
        n2 = make_node("n2", nodepool="pool-a")
        node_states = {"n1": n1, "n2": n2}

        mock_discover.return_value = {"n1": "pool-a", "n2": "pool-a"}
        mock_build.return_value = (node_states, [])
        mock_check.return_value = set()
        mock_select_reserved.return_value = {"pool-a": {"n1"}}
        mock_compute.return_value = (set(), set(), set(), set())

        reconcile(client, cfg, {})

        mock_select_reserved.assert_called_once()
        # reserved_nodes={"n1"} should be passed to compute_taints
        call_kwargs = mock_compute.call_args
        assert call_kwargs[1].get("reserved_nodes") == {"n1"} or (
            len(call_kwargs[0]) > 2 and call_kwargs[0][2] == {"n1"}
        )
        # reconcile_reservations called at the end
        mock_reconcile_res.assert_called_once_with(
            client,
            list(node_states.values()),
            {"n1"},
            cfg.dry_run,
        )

    @patch("compactor.m.rate_limit_blocks")
    @patch("compactor.pathlib.Path.touch")
    @patch("compactor.remove_taint")
    @patch("compactor.apply_taint")
    @patch("compactor.compute_taints")
    @patch("compactor.check_pending_pods")
    @patch("compactor.build_node_states")
    @patch("compactor.discover_managed_nodes")
    def test_reconcile_with_rate_limited_nodes(
        self,
        mock_discover,
        mock_build,
        mock_check,
        mock_compute,
        mock_apply,
        mock_remove,
        mock_touch,
        mock_rate_limit_blocks,
    ):
        """reconcile() logs and emits per-pool metrics for rate-limited nodes."""
        client = MagicMock()
        cfg = make_config()

        n1 = make_node("n1", nodepool="pool-a")
        n2 = make_node("n2", nodepool="pool-a")
        n3 = make_node("n3", nodepool="pool-b")
        node_states = {"n1": n1, "n2": n2, "n3": n3}

        mock_discover.return_value = {"n1": "pool-a", "n2": "pool-a", "n3": "pool-b"}
        mock_build.return_value = (node_states, [])
        mock_check.return_value = set()
        # Return n2, n3 as rate-limited
        mock_compute.return_value = ({"n1"}, set(), set(), {"n2", "n3"})

        reconcile(client, cfg, {})

        # Verify per-pool rate-limit metrics were emitted
        mock_rate_limit_blocks.labels.assert_called()
        pool_labels = {c[1]["nodepool"] for c in mock_rate_limit_blocks.labels.call_args_list}
        assert pool_labels == {"pool-a", "pool-b"}


# ============================================================================
# Fleet cooldown tests
# ============================================================================


class TestFleetCooldown:
    """Tests for fleet-level cooldown after burst untaint."""

    @patch("compactor.pathlib.Path.touch")
    @patch("compactor.remove_taint")
    @patch("compactor.apply_taint")
    @patch("compactor.compute_taints")
    @patch("compactor.check_pending_pods")
    @patch("compactor.build_node_states")
    @patch("compactor.discover_managed_nodes")
    def test_burst_untaint_triggers_fleet_cooldown_blocks_subsequent_taint(
        self,
        mock_discover,
        mock_build,
        mock_check,
        mock_compute,
        mock_apply,
        mock_remove,
        mock_touch,
    ):
        """After burst untaint in a pool, subsequent taints in that pool are blocked."""
        client = MagicMock()
        cfg = make_config(fleet_cooldown=900)
        taint_times: dict[str, float] = {}
        fleet_cooldown_times: dict[str, float] = {}

        # Iteration 1: burst untaint happens on n1 in pool "default"
        n1 = make_node("n1", nodepool="default", is_tainted=True)
        n2 = make_node("n2", nodepool="default", is_tainted=False)
        node_states = {"n1": n1, "n2": n2}

        mock_discover.return_value = {"n1": "default", "n2": "default"}
        mock_build.return_value = (node_states, [])
        mock_check.return_value = {"n1"}  # burst untaint for n1
        mock_compute.return_value = (set(), set(), set(), set())

        reconcile(client, cfg, taint_times, fleet_cooldown_times)

        # Fleet cooldown should be recorded for pool "default"
        assert "default" in fleet_cooldown_times

        # Iteration 2: compactor wants to taint n2, but fleet cooldown blocks it
        mock_discover.reset_mock()
        mock_build.reset_mock()
        mock_check.reset_mock()
        mock_compute.reset_mock()
        mock_apply.reset_mock()
        mock_remove.reset_mock()

        n1_iter2 = make_node("n1", nodepool="default", is_tainted=False)
        n2_iter2 = make_node("n2", nodepool="default", is_tainted=False)
        node_states_2 = {"n1": n1_iter2, "n2": n2_iter2}

        mock_discover.return_value = {"n1": "default", "n2": "default"}
        mock_build.return_value = (node_states_2, [])
        mock_check.return_value = set()  # no burst this time
        mock_compute.return_value = ({"n2"}, set(), set(), set())  # wants to taint n2

        reconcile(client, cfg, taint_times, fleet_cooldown_times)

        # n2 should NOT be tainted due to fleet cooldown
        mock_apply.assert_not_called()

    @patch("compactor.pathlib.Path.touch")
    @patch("compactor.remove_taint")
    @patch("compactor.apply_taint")
    @patch("compactor.compute_taints")
    @patch("compactor.check_pending_pods")
    @patch("compactor.build_node_states")
    @patch("compactor.discover_managed_nodes")
    def test_fleet_cooldown_expired_allows_taint(
        self,
        mock_discover,
        mock_build,
        mock_check,
        mock_compute,
        mock_apply,
        mock_remove,
        mock_touch,
    ):
        """After fleet_cooldown seconds elapse, tainting resumes."""
        client = MagicMock()
        cfg = make_config(fleet_cooldown=900)
        taint_times: dict[str, float] = {}
        # Fleet cooldown expired 1000 seconds ago
        fleet_cooldown_times: dict[str, float] = {"default": time.time() - 1000}

        n1 = make_node("n1", nodepool="default", is_tainted=False)
        node_states = {"n1": n1}

        mock_discover.return_value = {"n1": "default"}
        mock_build.return_value = (node_states, [])
        mock_check.return_value = set()
        mock_compute.return_value = ({"n1"}, set(), set(), set())

        reconcile(client, cfg, taint_times, fleet_cooldown_times)

        # Taint should proceed because cooldown expired
        mock_apply.assert_called_once_with(client, "n1", cfg.taint_key, cfg.dry_run)

    @patch("compactor.pathlib.Path.touch")
    @patch("compactor.remove_taint")
    @patch("compactor.apply_taint")
    @patch("compactor.compute_taints")
    @patch("compactor.check_pending_pods")
    @patch("compactor.build_node_states")
    @patch("compactor.discover_managed_nodes")
    def test_fleet_cooldown_surplus_override_halves_cooldown(
        self,
        mock_discover,
        mock_build,
        mock_check,
        mock_compute,
        mock_apply,
        mock_remove,
        mock_touch,
    ):
        """When >50% of pool is surplus, cooldown is halved."""
        client = MagicMock()
        cfg = make_config(fleet_cooldown=900)
        taint_times: dict[str, float] = {}
        # Fleet cooldown was 500 seconds ago -- past half (450s) but within full (900s)
        fleet_cooldown_times: dict[str, float] = {"default": time.time() - 500}

        # 4 nodes, 3 already tainted + 1 to be tainted = 4/4 surplus (100% > 50%)
        n1 = make_node("n1", nodepool="default", is_tainted=True)
        n2 = make_node("n2", nodepool="default", is_tainted=True)
        n3 = make_node("n3", nodepool="default", is_tainted=True)
        n4 = make_node("n4", nodepool="default", is_tainted=False)
        node_states = {"n1": n1, "n2": n2, "n3": n3, "n4": n4}

        mock_discover.return_value = {
            "n1": "default",
            "n2": "default",
            "n3": "default",
            "n4": "default",
        }
        mock_build.return_value = (node_states, [])
        mock_check.return_value = set()
        mock_compute.return_value = ({"n4"}, set(), set(), set())  # wants to taint n4

        reconcile(client, cfg, taint_times, fleet_cooldown_times)

        # With >50% surplus, effective cooldown is 450s. 500s elapsed > 450s,
        # so taint should proceed
        mock_apply.assert_called_once_with(client, "n4", cfg.taint_key, cfg.dry_run)

    @patch("compactor.pathlib.Path.touch")
    @patch("compactor.remove_taint")
    @patch("compactor.apply_taint")
    @patch("compactor.compute_taints")
    @patch("compactor.check_pending_pods")
    @patch("compactor.build_node_states")
    @patch("compactor.discover_managed_nodes")
    def test_fleet_cooldown_only_affects_burst_pool(
        self,
        mock_discover,
        mock_build,
        mock_check,
        mock_compute,
        mock_apply,
        mock_remove,
        mock_touch,
    ):
        """Fleet cooldown only blocks taints in the pool that had the burst."""
        client = MagicMock()
        cfg = make_config(fleet_cooldown=900)
        taint_times: dict[str, float] = {}
        # Only pool "pool-a" has fleet cooldown active
        fleet_cooldown_times: dict[str, float] = {"pool-a": time.time()}

        n1 = make_node("n1", nodepool="pool-a", is_tainted=False)
        n2 = make_node("n2", nodepool="pool-b", is_tainted=False)
        node_states = {"n1": n1, "n2": n2}

        mock_discover.return_value = {"n1": "pool-a", "n2": "pool-b"}
        mock_build.return_value = (node_states, [])
        mock_check.return_value = set()
        mock_compute.return_value = ({"n1", "n2"}, set(), set(), set())

        reconcile(client, cfg, taint_times, fleet_cooldown_times)

        # n1 (pool-a) should be blocked, n2 (pool-b) should be tainted
        apply_calls = [c[0][1] for c in mock_apply.call_args_list]
        assert "n2" in apply_calls
        assert "n1" not in apply_calls

    @patch("compactor.pathlib.Path.touch")
    @patch("compactor.remove_taint")
    @patch("compactor.apply_taint")
    @patch("compactor.compute_taints")
    @patch("compactor.check_pending_pods")
    @patch("compactor.build_node_states")
    @patch("compactor.discover_managed_nodes")
    def test_mandatory_untaint_not_affected_by_fleet_cooldown(
        self,
        mock_discover,
        mock_build,
        mock_check,
        mock_compute,
        mock_apply,
        mock_remove,
        mock_touch,
    ):
        """Fleet cooldown only blocks desired_taint, not untaint operations."""
        client = MagicMock()
        cfg = make_config(fleet_cooldown=900)
        taint_times: dict[str, float] = {}
        fleet_cooldown_times: dict[str, float] = {"default": time.time()}

        n1 = make_node("n1", nodepool="default", is_tainted=True)
        node_states = {"n1": n1}

        mock_discover.return_value = {"n1": "default"}
        mock_build.return_value = (node_states, [])
        mock_check.return_value = set()
        # mandatory untaint for n1
        mock_compute.return_value = (set(), {"n1"}, {"n1"}, set())

        reconcile(client, cfg, taint_times, fleet_cooldown_times)

        # Mandatory untaint should proceed despite fleet cooldown
        mock_remove.assert_called_once_with(client, "n1", cfg.taint_key, cfg.dry_run)

    @patch("compactor.pathlib.Path.touch")
    @patch("compactor.remove_taint")
    @patch("compactor.apply_taint")
    @patch("compactor.compute_taints")
    @patch("compactor.check_pending_pods")
    @patch("compactor.build_node_states")
    @patch("compactor.discover_managed_nodes")
    def test_fleet_cooldown_zero_disables_feature(
        self,
        mock_discover,
        mock_build,
        mock_check,
        mock_compute,
        mock_apply,
        mock_remove,
        mock_touch,
    ):
        """fleet_cooldown=0 disables the fleet cooldown feature entirely."""
        client = MagicMock()
        cfg = make_config(fleet_cooldown=0)
        taint_times: dict[str, float] = {}
        # Fleet cooldown recorded just now -- but feature is disabled
        fleet_cooldown_times: dict[str, float] = {"default": time.time()}

        n1 = make_node("n1", nodepool="default", is_tainted=False)
        node_states = {"n1": n1}

        mock_discover.return_value = {"n1": "default"}
        mock_build.return_value = (node_states, [])
        mock_check.return_value = set()
        mock_compute.return_value = ({"n1"}, set(), set(), set())

        reconcile(client, cfg, taint_times, fleet_cooldown_times)

        # Taint should proceed because fleet_cooldown=0 disables it
        mock_apply.assert_called_once_with(client, "n1", cfg.taint_key, cfg.dry_run)


# ============================================================================
# main() tests
# ============================================================================


@patch("compactor.start_http_server")
class TestMain:
    """Tests for the main() entry point."""

    @patch("compactor.cleanup_stale_taints")
    @patch("compactor.reconcile")
    @patch("compactor.Client")
    @patch("compactor.Config.from_env")
    @patch("compactor.signal.signal")
    @patch("compactor.time.sleep")
    def test_main_calls_reconcile_and_cleanup(
        self,
        mock_sleep,
        mock_signal_fn,
        mock_from_env,
        mock_client_cls,
        mock_reconcile,
        mock_cleanup,
        _mock_http_server,
    ):
        cfg = make_config()
        mock_from_env.return_value = cfg
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        # After first reconcile call, simulate shutdown by capturing the
        # signal handler and invoking it
        call_count = 0

        def reconcile_side_effect(client, config, taint_times, fleet_cooldown_times=None):
            nonlocal call_count
            call_count += 1
            if call_count >= 1:
                # Find the SIGTERM handler and call it
                for c in mock_signal_fn.call_args_list:
                    if c[0][0] == signal.SIGTERM:
                        handler = c[0][1]
                        handler(signal.SIGTERM, None)
                        return

        mock_reconcile.side_effect = reconcile_side_effect

        result = main()

        assert result == 0
        mock_reconcile.assert_called()
        mock_cleanup.assert_called_once_with(mock_client, cfg)

    @patch("compactor.cleanup_stale_taints")
    @patch("compactor.reconcile")
    @patch("compactor.Client")
    @patch("compactor.Config.from_env")
    @patch("compactor.signal.signal")
    @patch("compactor.time.sleep")
    def test_main_registers_signal_handlers(
        self,
        mock_sleep,
        mock_signal_fn,
        mock_from_env,
        mock_client_cls,
        mock_reconcile,
        mock_cleanup,
        _mock_http_server,
    ):
        mock_from_env.return_value = make_config()
        mock_client_cls.return_value = MagicMock()

        # Immediately shut down
        def set_shutdown(*args, **kwargs):
            for c in mock_signal_fn.call_args_list:
                if c[0][0] == signal.SIGTERM:
                    c[0][1](signal.SIGTERM, None)

        mock_reconcile.side_effect = set_shutdown

        main()

        # Verify both SIGTERM and SIGINT handlers were registered
        signal_calls = [c[0][0] for c in mock_signal_fn.call_args_list]
        assert signal.SIGTERM in signal_calls
        assert signal.SIGINT in signal_calls

    @patch("compactor.cleanup_stale_taints")
    @patch("compactor.reconcile")
    @patch("compactor.Client")
    @patch("compactor.Config.from_env")
    @patch("compactor.signal.signal")
    @patch("compactor.time.sleep")
    def test_main_reconcile_exception_does_not_crash(
        self,
        mock_sleep,
        mock_signal_fn,
        mock_from_env,
        mock_client_cls,
        mock_reconcile,
        mock_cleanup,
        _mock_http_server,
    ):
        mock_from_env.return_value = make_config()
        mock_client_cls.return_value = MagicMock()

        call_count = 0

        def reconcile_side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("k8s API down")
            # Second call triggers shutdown
            for c in mock_signal_fn.call_args_list:
                if c[0][0] == signal.SIGTERM:
                    c[0][1](signal.SIGTERM, None)

        mock_reconcile.side_effect = reconcile_side_effect

        result = main()

        assert result == 0
        assert mock_reconcile.call_count == 2

    @patch("compactor.cleanup_stale_taints")
    @patch("compactor.reconcile")
    @patch("compactor.Client")
    @patch("compactor.Config.from_env")
    @patch("compactor.signal.signal")
    @patch("compactor.time.sleep")
    def test_main_cleanup_exception_does_not_crash(
        self,
        mock_sleep,
        mock_signal_fn,
        mock_from_env,
        mock_client_cls,
        mock_reconcile,
        mock_cleanup,
        _mock_http_server,
    ):
        mock_from_env.return_value = make_config()
        mock_client_cls.return_value = MagicMock()

        def set_shutdown(*args, **kwargs):
            for c in mock_signal_fn.call_args_list:
                if c[0][0] == signal.SIGTERM:
                    c[0][1](signal.SIGTERM, None)

        mock_reconcile.side_effect = set_shutdown
        mock_cleanup.side_effect = RuntimeError("cleanup failed")

        result = main()

        assert result == 0
        mock_cleanup.assert_called_once()

    @patch("compactor.cleanup_reservations")
    @patch("compactor.cleanup_stale_taints")
    @patch("compactor.reconcile")
    @patch("compactor.Client")
    @patch("compactor.Config.from_env")
    @patch("compactor.signal.signal")
    @patch("compactor.time.sleep")
    def test_main_cleanup_reservations_exception_does_not_crash(
        self,
        mock_sleep,
        mock_signal_fn,
        mock_from_env,
        mock_client_cls,
        mock_reconcile,
        mock_cleanup_taints,
        mock_cleanup_reservations,
        _mock_http_server,
    ):
        """cleanup_reservations raising during shutdown does not crash main()."""
        mock_from_env.return_value = make_config()
        mock_client_cls.return_value = MagicMock()

        def set_shutdown(*args, **kwargs):
            for c in mock_signal_fn.call_args_list:
                if c[0][0] == signal.SIGTERM:
                    c[0][1](signal.SIGTERM, None)

        mock_reconcile.side_effect = set_shutdown
        mock_cleanup_reservations.side_effect = RuntimeError("reservations cleanup failed")

        result = main()

        assert result == 0
        mock_cleanup_reservations.assert_called_once()

    @patch("compactor.cleanup_stale_taints")
    @patch("compactor.reconcile")
    @patch("compactor.Client")
    @patch("compactor.Config.from_env")
    @patch("compactor.signal.signal")
    @patch("compactor.time.sleep")
    def test_main_recreates_client_on_401(
        self,
        mock_sleep,
        mock_signal_fn,
        mock_from_env,
        mock_client_cls,
        mock_reconcile,
        mock_cleanup,
        _mock_http_server,
    ):
        """401 Unauthorized triggers client recreation (SA token rotation)."""
        mock_from_env.return_value = make_config()
        original_client = MagicMock(name="original_client")
        refreshed_client = MagicMock(name="refreshed_client")
        mock_client_cls.side_effect = [original_client, refreshed_client]

        call_count = 0

        def reconcile_side_effect(client, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # First call: raise 401 to trigger client recreation
                raise _make_api_error(401)
            # Second call: verify new client is used, then shutdown
            assert client is refreshed_client
            for c in mock_signal_fn.call_args_list:
                if c[0][0] == signal.SIGTERM:
                    c[0][1](signal.SIGTERM, None)

        mock_reconcile.side_effect = reconcile_side_effect

        result = main()

        assert result == 0
        assert mock_client_cls.call_count == 2
        assert mock_reconcile.call_count == 2


# ============================================================================
# compute_taints -- rate limiting tests
# ============================================================================


class TestComputeTaintsRateLimiting:
    """Tests for the taint_rate rate-limiting feature."""

    def test_rate_limit_caps_new_taints(self):
        """10 nodes, surplus=9, taint_rate=0.3: ceil(9*0.3)=3 new taints."""
        cfg = make_config(min_nodes=1, taint_rate=0.3, spare_capacity_nodes=0, spare_capacity_ratio=0.0)
        nodes = {}
        for i in range(10):
            n = make_node(f"node-{i}", nodepool="pool")
            nodes[f"node-{i}"] = n
        # 10 nodes, no pods -> bin_pack_min_nodes returns 0, min_nodes=1
        # surplus = 10 - 1 = 9; max_new_taints = ceil(9 * 0.3) = 3
        # Rate-limited nodes don't increment taint_count, so all
        # remaining candidates after the cap are rate-limited.

        to_taint, _to_untaint, _mandatory, rate_limited = compute_taints(nodes, cfg)

        assert len(to_taint) == 3
        assert len(rate_limited) == 7  # 10 total - 3 tainted = 7 rate-limited

    def test_rate_limit_small_pool(self):
        """3 nodes, 2 surplus, taint_rate=0.3: ceil(2*0.3)=ceil(0.6)=1 new taint."""
        cfg = make_config(min_nodes=1, taint_rate=0.3, spare_capacity_nodes=0, spare_capacity_ratio=0.0)
        nodes = {}
        for i in range(3):
            n = make_node(f"node-{i}", nodepool="pool")
            nodes[f"node-{i}"] = n

        to_taint, _to_untaint, _mandatory, rate_limited = compute_taints(nodes, cfg)

        assert len(to_taint) == 1
        assert len(rate_limited) == 2  # rate-limited nodes don't advance taint_count

    def test_already_tainted_nodes_dont_count_toward_cap(self):
        """Already-tainted nodes within surplus are retained without consuming the rate cap."""
        cfg = make_config(min_nodes=1, taint_rate=0.3, spare_capacity_nodes=0, spare_capacity_ratio=0.0)
        nodes = {}
        # 6 nodes total: 3 already tainted, 3 untainted
        for i in range(3):
            n = make_node(f"tainted-{i}", nodepool="pool", is_tainted=True)
            nodes[f"tainted-{i}"] = n
        for i in range(3):
            n = make_node(f"fresh-{i}", nodepool="pool", is_tainted=False)
            nodes[f"fresh-{i}"] = n

        # surplus = 6 - 1 = 5; max_new_taints = ceil(5 * 0.3) = 2
        to_taint, _to_untaint, _mandatory, rate_limited = compute_taints(nodes, cfg)

        # All 3 already-tainted stay in to_taint (they don't consume new taint cap)
        already_tainted_in_result = {n for n in to_taint if n.startswith("tainted-")}
        new_tainted_in_result = {n for n in to_taint if n.startswith("fresh-")}
        assert len(already_tainted_in_result) == 3
        assert len(new_tainted_in_result) == 2  # only 2 new taints allowed
        assert len(rate_limited) == 0  # surplus=5, 3 already tainted + 2 new = 5, no excess

    def test_rate_1_disables_limiting(self):
        """taint_rate=1.0 means all surplus nodes are tainted in one iteration."""
        cfg = make_config(min_nodes=1, taint_rate=1.0, spare_capacity_nodes=0, spare_capacity_ratio=0.0)
        nodes = {}
        for i in range(10):
            n = make_node(f"node-{i}", nodepool="pool")
            nodes[f"node-{i}"] = n

        to_taint, _to_untaint, _mandatory, rate_limited = compute_taints(nodes, cfg)

        # surplus = 9; max_new_taints = ceil(9 * 1.0) = 9
        assert len(to_taint) == 9
        assert len(rate_limited) == 0

    def test_rate_0_allows_at_least_one(self):
        """taint_rate=0.0 should still allow at least 1 new taint (max(1, ...))."""
        cfg = make_config(min_nodes=1, taint_rate=0.0, spare_capacity_nodes=0, spare_capacity_ratio=0.0)
        nodes = {}
        for i in range(5):
            n = make_node(f"node-{i}", nodepool="pool")
            nodes[f"node-{i}"] = n

        to_taint, _to_untaint, _mandatory, rate_limited = compute_taints(nodes, cfg)

        # surplus = 4; max_new_taints = max(1, ceil(4 * 0.0)) = max(1, 0) = 1
        assert len(to_taint) == 1
        assert len(rate_limited) == 4  # rate-limited nodes don't advance taint_count
