"""Unit tests for the node compactor controller."""

import math
import signal
import time
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from compactor import _fleet_group_key, main, reconcile
from lightkube import ApiError
from models import (
    LABEL_NODE_FLEET,
    PEAK_WINDOW_SECONDS,
    Config,
    NodeState,
    PodInfo,
    parse_cpu,
    parse_memory,
)
from packing import _pods_fit_on_nodes, bin_pack_min_nodes, compute_taints
from pending import pending_pods_for_group

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
    gpu: int = 0,
    is_tainted: bool = False,
    creation_time: datetime | None = None,
) -> NodeState:
    return NodeState(
        name=name,
        nodepool=nodepool,
        allocatable_cpu=cpu,
        allocatable_memory=mem,
        allocatable_gpu=gpu,
        creation_time=creation_time or NOW - timedelta(hours=1),
        is_tainted=is_tainted,
    )


def make_pod(
    name: str = "pod",
    cpu: float = 1.0,
    mem: int = 4 * GiB,
    gpu: int = 0,
    node_name: str = "node-1",
    is_daemonset: bool = False,
    start_time: datetime | None = None,
) -> PodInfo:
    return PodInfo(
        name=name,
        namespace="default",
        cpu_request=cpu,
        memory_request=mem,
        gpu_request=gpu,
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
# Fleet-aware helpers
# ============================================================================


def make_fleet_node(name, nodepool, fleet, cpu=16.0, mem=64 * GiB, gpu=0, is_tainted=False, creation_time=None):
    """Create a NodeState with a node-fleet label for fleet-aware tests."""
    node = make_node(
        name, nodepool=nodepool, cpu=cpu, mem=mem, gpu=gpu, is_tainted=is_tainted, creation_time=creation_time
    )
    node.labels[LABEL_NODE_FLEET] = fleet
    return node


# ============================================================================
# Fleet capacity tests (compute_taints with group_key)
# ============================================================================


class TestFleetCapacity:
    """Tests for fleet-aware grouping via the group_key parameter."""

    def test_fleet_groups_multiple_nodepools(self):
        """Nodes in different NodePools but same fleet are grouped together.

        Fleet "m8g" has a 16-CPU node (m8g-8xlarge) and a 192-CPU node
        (m8g-48xlarge). One pod needs 10 CPU. Fleet needs 1 node -> surplus=1.
        The 16-CPU node should be tainted (smaller, less capacity).
        """
        cfg = make_config(min_nodes=1, spare_capacity_nodes=0, spare_capacity_ratio=0.0)
        n_small = make_fleet_node("n-small", nodepool="m8g-8xlarge", fleet="m8g", cpu=16.0)
        n_big = make_fleet_node("n-big", nodepool="m8g-48xlarge", fleet="m8g", cpu=192.0)
        n_big.pods = [make_pod("p1", cpu=10.0, node_name="n-big")]

        nodes = {"n-small": n_small, "n-big": n_big}

        to_taint, _to_untaint, _mandatory, _rate_limited = compute_taints(nodes, cfg, group_key=_fleet_group_key)

        assert "n-small" in to_taint, "Smaller node in fleet should be tainted"
        assert "n-big" not in to_taint, "Larger node with workload should not be tainted"

    def test_fleet_surplus_across_pools(self):
        """3 nodes in 3 different NodePools, all fleet 'r7a'. Pods fit on 1 node.

        Surplus=2, so 2 nodes should be tainted.
        """
        cfg = make_config(min_nodes=1, spare_capacity_nodes=0, spare_capacity_ratio=0.0)
        n1 = make_fleet_node("n1", nodepool="r7a-4xlarge", fleet="r7a", cpu=192.0)
        n2 = make_fleet_node("n2", nodepool="r7a-8xlarge", fleet="r7a", cpu=192.0)
        n3 = make_fleet_node("n3", nodepool="r7a-16xlarge", fleet="r7a", cpu=192.0)
        n1.pods = [make_pod("p1", cpu=100.0, node_name="n1")]

        nodes = {"n1": n1, "n2": n2, "n3": n3}

        to_taint, _to_untaint, _mandatory, _rate_limited = compute_taints(nodes, cfg, group_key=_fleet_group_key)

        assert len(to_taint) == 2, "Surplus is 2, so 2 nodes should be tainted"

    def test_non_fleet_nodes_grouped_by_nodepool(self):
        """Nodes without 'node-fleet' label fall back to nodepool grouping.

        Two nodepools, 2 nodes each, no fleet labels. Each pool independent.
        """
        cfg = make_config(min_nodes=1, spare_capacity_nodes=0, spare_capacity_ratio=0.0)
        n1 = make_node("n1", nodepool="pool-a", cpu=16.0)
        n2 = make_node("n2", nodepool="pool-a", cpu=16.0)
        n3 = make_node("n3", nodepool="pool-b", cpu=16.0)
        n4 = make_node("n4", nodepool="pool-b", cpu=16.0)
        nodes = {"n1": n1, "n2": n2, "n3": n3, "n4": n4}

        to_taint, _to_untaint, _mandatory, _rate_limited = compute_taints(nodes, cfg, group_key=_fleet_group_key)

        # No fleet label -> group_key returns nodepool -> same behavior as before.
        pool_a_tainted = to_taint & {"n1", "n2"}
        pool_b_tainted = to_taint & {"n3", "n4"}
        assert len(pool_a_tainted) == 1
        assert len(pool_b_tainted) == 1

    def test_mixed_fleet_and_non_fleet(self):
        """Fleet nodes and non-fleet nodes are grouped independently.

        Fleet "m8g" has 2 nodes, nodepool "standalone" has 2 nodes (no fleet).
        Each group processes independently.
        """
        cfg = make_config(min_nodes=1, spare_capacity_nodes=0, spare_capacity_ratio=0.0)
        f1 = make_fleet_node("f1", nodepool="m8g-8xl", fleet="m8g", cpu=16.0)
        f2 = make_fleet_node("f2", nodepool="m8g-48xl", fleet="m8g", cpu=192.0)
        s1 = make_node("s1", nodepool="standalone", cpu=16.0)
        s2 = make_node("s2", nodepool="standalone", cpu=16.0)

        nodes = {"f1": f1, "f2": f2, "s1": s1, "s2": s2}

        to_taint, _to_untaint, _mandatory, _rate_limited = compute_taints(nodes, cfg, group_key=_fleet_group_key)

        # Fleet "m8g": 2 nodes, no pods, surplus=1 -> 1 tainted
        fleet_tainted = to_taint & {"f1", "f2"}
        assert len(fleet_tainted) == 1

        # Nodepool "standalone": 2 nodes, no pods, surplus=1 -> 1 tainted
        standalone_tainted = to_taint & {"s1", "s2"}
        assert len(standalone_tainted) == 1

    def test_fleet_bin_packing_heterogeneous_sizes(self):
        """Fleet with 16-CPU and 192-CPU nodes. Pods need 180 CPU total.

        bin_pack_min_nodes should correctly account for heterogeneous capacity.
        180 CPU does not fit on the 16-CPU node alone, so the 192-CPU node
        is needed. With 2 nodes and min_needed=1, surplus=1.
        The 16-CPU node should be tainted (smaller capacity).
        """
        cfg = make_config(min_nodes=1, spare_capacity_nodes=0, spare_capacity_ratio=0.0)
        n_small = make_fleet_node("n-small", nodepool="m8g-8xlarge", fleet="m8g", cpu=16.0)
        n_big = make_fleet_node("n-big", nodepool="m8g-48xlarge", fleet="m8g", cpu=192.0)
        # Place pods totalling 180 CPU on the big node
        n_big.pods = [make_pod(f"p{i}", cpu=20.0, node_name="n-big") for i in range(9)]

        nodes = {"n-small": n_small, "n-big": n_big}

        to_taint, _to_untaint, _mandatory, _rate_limited = compute_taints(nodes, cfg, group_key=_fleet_group_key)

        # bin_pack: 9 pods * 20 CPU = 180 total. n_big has 192 CPU, n_small has 16.
        # FFD: biggest bin first (192), all 9 pods fit on n_big. min_needed=1.
        # surplus=1. n_small tainted.
        assert "n-small" in to_taint
        assert "n-big" not in to_taint

    def test_fleet_safety_check_cross_pool(self):
        """Pods on a 16-CPU node that fit on a 192-CPU node in the same fleet.

        Safety check should consider remaining capacity across different
        NodePools within the same fleet.
        """
        cfg = make_config(min_nodes=1, spare_capacity_nodes=0, spare_capacity_ratio=0.0)
        n_small = make_fleet_node("n-small", nodepool="m8g-8xlarge", fleet="m8g", cpu=16.0)
        n_small.pods = [make_pod("p-small", cpu=10.0, node_name="n-small")]
        n_big = make_fleet_node("n-big", nodepool="m8g-48xlarge", fleet="m8g", cpu=192.0)
        n_big.pods = [make_pod("p-big", cpu=50.0, node_name="n-big")]

        nodes = {"n-small": n_small, "n-big": n_big}

        to_taint, _to_untaint, _mandatory, _rate_limited = compute_taints(nodes, cfg, group_key=_fleet_group_key)

        # bin_pack: 2 pods (10+50=60 CPU). n_big has 192 CPU -> fits on 1 node.
        # surplus=1. n_small is the candidate (lower CPU, lower util).
        # Safety: n_small's pod (10 CPU) can fit on n_big (192-50=142 free).
        # Taint allowed.
        assert "n-small" in to_taint
        assert "n-big" not in to_taint

    def test_group_key_none_uses_nodepool(self):
        """Calling compute_taints with group_key=None groups by nodepool (backward compat).

        Two NodePools, each with 2 nodes. Without fleet grouping, each pool
        is independent.
        """
        cfg = make_config(min_nodes=1, spare_capacity_nodes=0, spare_capacity_ratio=0.0)
        n1 = make_node("n1", nodepool="pool-a", cpu=16.0)
        n2 = make_node("n2", nodepool="pool-a", cpu=16.0)
        n3 = make_node("n3", nodepool="pool-b", cpu=16.0)
        n4 = make_node("n4", nodepool="pool-b", cpu=16.0)
        nodes = {"n1": n1, "n2": n2, "n3": n3, "n4": n4}

        to_taint, _to_untaint, _mandatory, _rate_limited = compute_taints(nodes, cfg, group_key=None)

        pool_a_tainted = to_taint & {"n1", "n2"}
        pool_b_tainted = to_taint & {"n3", "n4"}
        assert len(pool_a_tainted) == 1
        assert len(pool_b_tainted) == 1


# ============================================================================
# Taint priority tests (allocatable_cpu in sort key)
# ============================================================================


class TestTaintPriority:
    """Tests for the updated taint priority that includes allocatable_cpu."""

    def test_taint_priority_smallest_first(self):
        """Fleet with 16-CPU and 192-CPU nodes, both idle.

        Smallest allocatable_cpu should be tainted first since it has less
        capacity to offer.
        """
        cfg = make_config(min_nodes=1, spare_capacity_nodes=0, spare_capacity_ratio=0.0)
        n_small = make_fleet_node("n-small", nodepool="m8g-8xlarge", fleet="m8g", cpu=16.0)
        n_big = make_fleet_node("n-big", nodepool="m8g-48xlarge", fleet="m8g", cpu=192.0)

        nodes = {"n-small": n_small, "n-big": n_big}

        to_taint, _to_untaint, _mandatory, _rate_limited = compute_taints(nodes, cfg, group_key=_fleet_group_key)

        # Both idle, surplus=1. n_small has lower allocatable_cpu -> tainted first.
        assert "n-small" in to_taint
        assert "n-big" not in to_taint

    def test_taint_priority_old_still_wins(self):
        """Large old node vs small young node. Old node tainted first.

        The -is_old component has the highest priority in the sort key.
        """
        cfg = make_config(
            min_nodes=1,
            max_uptime_hours=24,
            spare_capacity_nodes=0,
            spare_capacity_ratio=0.0,
        )
        n_big_old = make_fleet_node(
            "n-big-old",
            nodepool="m8g-48xlarge",
            fleet="m8g",
            cpu=192.0,
            creation_time=NOW - timedelta(hours=50),
        )
        n_small_young = make_fleet_node(
            "n-small-young",
            nodepool="m8g-8xlarge",
            fleet="m8g",
            cpu=16.0,
            creation_time=NOW - timedelta(hours=2),
        )

        nodes = {"n-big-old": n_big_old, "n-small-young": n_small_young}

        to_taint, _to_untaint, _mandatory, _rate_limited = compute_taints(nodes, cfg, group_key=_fleet_group_key)

        # Old node (>24h) has is_old=1 -> -is_old=-1 -> sorts first.
        # Despite having more CPU, old wins the priority.
        assert "n-big-old" in to_taint
        assert "n-small-young" not in to_taint

    def test_taint_priority_same_cpu_falls_through_to_utilization(self):
        """Two nodes with same allocatable_cpu. Lower utilization tainted first.

        When is_old and allocatable_cpu are equal, utilization determines order.
        """
        cfg = make_config(min_nodes=1, spare_capacity_nodes=0, spare_capacity_ratio=0.0)
        n_low_util = make_fleet_node("n-low-util", nodepool="m8g-8xlarge", fleet="m8g", cpu=16.0)
        n_low_util.pods = [make_pod("p1", cpu=2.0, node_name="n-low-util")]

        n_high_util = make_fleet_node("n-high-util", nodepool="m8g-8xlarge-v2", fleet="m8g", cpu=16.0)
        n_high_util.pods = [make_pod("p2", cpu=12.0, node_name="n-high-util")]

        n_keep = make_fleet_node("n-keep", nodepool="m8g-8xlarge-v3", fleet="m8g", cpu=16.0)
        n_keep.pods = [make_pod("p3", cpu=14.0, node_name="n-keep")]

        nodes = {"n-low-util": n_low_util, "n-high-util": n_high_util, "n-keep": n_keep}

        to_taint, _to_untaint, _mandatory, _rate_limited = compute_taints(nodes, cfg, group_key=_fleet_group_key)

        # 3 nodes. bin_pack: 3 pods (2+12+14=28 CPU). 16 per node -> need 2.
        # surplus=1. Same CPU -> lower utilization tainted first.
        assert "n-low-util" in to_taint
        assert len(to_taint) == 1


# ============================================================================
# GPU packing tests
# ============================================================================


class TestGPUPacking:
    """Tests for 3D bin-packing with GPU dimension."""

    def test_gpu_pods_fit_on_gpu_node(self):
        """A 4-GPU pod fits on an 8-GPU node -> min_nodes = 1."""
        nodes = [make_node("n1", cpu=96.0, mem=256 * GiB, gpu=8)]
        pods = [make_pod("p1", cpu=4.0, mem=16 * GiB, gpu=4)]
        assert bin_pack_min_nodes(pods, nodes) == 1

    def test_gpu_pods_dont_fit_without_gpu(self):
        """A 1-GPU pod cannot fit on a CPU-only node (gpu=0)."""
        nodes = [make_node("n1", cpu=96.0, mem=256 * GiB, gpu=0)]
        pods = [make_pod("p1", cpu=4.0, mem=16 * GiB, gpu=1)]
        # Pod needs GPU but node has none -> can't place, return len(bins)=1
        assert bin_pack_min_nodes(pods, nodes) == 1

    def test_mixed_gpu_packing(self):
        """8-GPU node, one 4-GPU pod + two 1-GPU pods -> all fit on 1 node."""
        nodes = [make_node("n1", cpu=96.0, mem=256 * GiB, gpu=8)]
        pods = [
            make_pod("p1", cpu=4.0, mem=16 * GiB, gpu=4),
            make_pod("p2", cpu=2.0, mem=8 * GiB, gpu=1),
            make_pod("p3", cpu=2.0, mem=8 * GiB, gpu=1),
        ]
        # 6 GPUs used out of 8, fits on 1 node
        assert bin_pack_min_nodes(pods, nodes) == 1

    def test_gpu_exhaustion_needs_second_node(self):
        """Three 4-GPU pods need 12 GPUs total, 8-GPU nodes -> 2 nodes."""
        nodes = [make_node(f"n{i}", cpu=96.0, mem=256 * GiB, gpu=8) for i in range(3)]
        pods = [make_pod(f"p{i}", cpu=4.0, mem=16 * GiB, gpu=4) for i in range(3)]
        # 12 GPUs total: 2 pods on node1 (8 GPU), 1 pod on node2 (4 GPU)
        assert bin_pack_min_nodes(pods, nodes) == 2

    def test_gpu_sort_priority(self):
        """Pods sorted by GPU descending first -- 4-GPU pod placed before high-CPU 0-GPU pod."""
        nodes = [make_node("n1", cpu=96.0, mem=256 * GiB, gpu=8)]
        pods = [
            make_pod("high-cpu", cpu=80.0, mem=16 * GiB, gpu=0),
            make_pod("gpu-pod", cpu=4.0, mem=16 * GiB, gpu=4),
        ]
        # Both fit on the same 8-GPU, 96-CPU node. Sorted by (gpu, cpu) desc:
        # gpu-pod (4,4) > high-cpu (0,80). Both placed on n1. 1 node needed.
        assert bin_pack_min_nodes(pods, nodes) == 1

    def test_non_gpu_pods_on_gpu_node(self):
        """CPU-only pods fit on GPU nodes regardless of GPU capacity."""
        nodes = [make_node("n1", cpu=96.0, mem=256 * GiB, gpu=8)]
        pods = [make_pod(f"p{i}", cpu=8.0, mem=16 * GiB, gpu=0) for i in range(4)]
        # 32 CPU out of 96, no GPU demand -> all fit on 1 node
        assert bin_pack_min_nodes(pods, nodes) == 1

    def test_pods_fit_on_nodes_gpu(self):
        """_pods_fit_on_nodes checks GPU dimension -- GPU pods from a taint
        candidate must fit on remaining GPU nodes."""
        # Remaining node has 8 GPUs, 4 already used
        remaining = make_node("remain", cpu=96.0, mem=256 * GiB, gpu=8)
        remaining.pods = [make_pod("existing", cpu=4.0, mem=8 * GiB, gpu=4, node_name="remain")]

        # Displaced pod needs 3 GPUs -> 4 remaining on the node -> fits
        displaced = [make_pod("disp", cpu=4.0, mem=8 * GiB, gpu=3)]
        assert _pods_fit_on_nodes(displaced, [remaining]) is True

        # Displaced pod needs 5 GPUs -> only 4 remaining -> doesn't fit
        displaced_big = [make_pod("disp-big", cpu=4.0, mem=8 * GiB, gpu=5)]
        assert _pods_fit_on_nodes(displaced_big, [remaining]) is False

        # Displaced pod needs GPU but remaining node has no GPU -> doesn't fit
        cpu_only = make_node("cpu-only", cpu=96.0, mem=256 * GiB, gpu=0)
        displaced_gpu = [make_pod("disp-gpu", cpu=4.0, mem=8 * GiB, gpu=1)]
        assert _pods_fit_on_nodes(displaced_gpu, [cpu_only]) is False


# ============================================================================
# GPU utilization tests
# ============================================================================


class TestGPUUtilization:
    """Tests for GPU utilization properties on NodeState."""

    def test_gpu_utilization_property(self):
        """8-GPU node, 4-GPU workload pod -> gpu_utilization = 0.5."""
        node = make_node("n1", cpu=96.0, mem=256 * GiB, gpu=8)
        node.pods = [make_pod("p1", cpu=4.0, mem=8 * GiB, gpu=4, node_name="n1")]
        assert node.gpu_utilization == pytest.approx(0.5)

    def test_utilization_includes_gpu(self):
        """8-GPU node with low CPU but high GPU -> utilization = GPU value."""
        node = make_node("n1", cpu=96.0, mem=256 * GiB, gpu=8)
        # Low CPU (10%), low memory, but high GPU (87.5%)
        node.pods = [make_pod("p1", cpu=10.0, mem=8 * GiB, gpu=7, node_name="n1")]
        assert node.gpu_utilization == pytest.approx(7.0 / 8.0)
        assert node.utilization == pytest.approx(7.0 / 8.0)

    def test_utilization_ignores_gpu_on_cpu_node(self):
        """CPU-only node -> utilization is max(cpu, mem), GPU not included."""
        node = make_node("n1", cpu=16.0, mem=64 * GiB, gpu=0)
        node.pods = [make_pod("p1", cpu=12.0, mem=8 * GiB, node_name="n1")]
        # CPU utilization = 12/16 = 0.75, memory = 8/64 = 0.125
        assert node.gpu_utilization == 0.0
        assert node.utilization == pytest.approx(0.75)


# ============================================================================
# GPU taint priority tests
# ============================================================================


class TestGPUTaintPriority:
    """Tests for taint priority sort including allocatable_gpu."""

    def test_taint_priority_keeps_larger_gpu_node(self):
        """Two nodes same CPU, one with 8 GPU, one with 1 GPU.
        The 1-GPU node is tainted first (lower allocatable_gpu in sort key)."""
        cfg = make_config(min_nodes=1, spare_capacity_nodes=0, spare_capacity_ratio=0.0)
        n_big_gpu = make_fleet_node("n-big-gpu", nodepool="gpu-8", fleet="gpu", cpu=96.0, gpu=8)
        n_small_gpu = make_fleet_node("n-small-gpu", nodepool="gpu-1", fleet="gpu", cpu=96.0, gpu=1)

        nodes = {"n-big-gpu": n_big_gpu, "n-small-gpu": n_small_gpu}

        to_taint, _to_untaint, _mandatory, _rate_limited = compute_taints(nodes, cfg, group_key=_fleet_group_key)

        # Both idle, surplus=1. n_small_gpu has lower allocatable_gpu -> tainted first.
        assert "n-small-gpu" in to_taint
        assert "n-big-gpu" not in to_taint

    def test_gpu_fleet_surplus_taints_correct_node(self):
        """Fleet with 3 GPU nodes, only 1 GPU pod -> surplus should taint
        the least-utilized/smallest nodes."""
        cfg = make_config(min_nodes=1, spare_capacity_nodes=0, spare_capacity_ratio=0.0)
        n1 = make_fleet_node("n1", nodepool="gpu-8", fleet="gpu", cpu=96.0, gpu=8)
        n1.pods = [make_pod("p1", cpu=4.0, mem=8 * GiB, gpu=4, node_name="n1")]
        n2 = make_fleet_node("n2", nodepool="gpu-8", fleet="gpu", cpu=96.0, gpu=8)
        n3 = make_fleet_node("n3", nodepool="gpu-8", fleet="gpu", cpu=96.0, gpu=8)

        nodes = {"n1": n1, "n2": n2, "n3": n3}

        to_taint, _to_untaint, _mandatory, _rate_limited = compute_taints(nodes, cfg, group_key=_fleet_group_key)

        # 1 pod (4 GPU, 4 CPU). bin_pack: fits on 1 node. surplus=2.
        # n2 and n3 are idle (lower utilization) -> tainted.
        assert len(to_taint) == 2
        assert "n1" not in to_taint

    def test_gpu_safety_check_prevents_stranding(self):
        """Taint candidate has GPU pods. Remaining nodes have enough CPU/memory
        but no GPU -> safety check prevents tainting (pods can't fit)."""
        cfg = make_config(min_nodes=1, spare_capacity_nodes=0, spare_capacity_ratio=0.0)
        # GPU node with a GPU pod
        n_gpu = make_node("n-gpu", nodepool="mixed", cpu=96.0, mem=256 * GiB, gpu=8)
        n_gpu.pods = [make_pod("gpu-pod", cpu=4.0, mem=8 * GiB, gpu=4, node_name="n-gpu")]
        # CPU-only node (no GPU)
        n_cpu = make_node("n-cpu", nodepool="mixed", cpu=96.0, mem=256 * GiB, gpu=0)

        nodes = {"n-gpu": n_gpu, "n-cpu": n_cpu}

        to_taint, _to_untaint, _mandatory, _rate_limited = compute_taints(nodes, cfg)

        # bin_pack: 1 pod (4 CPU, 4 GPU). Needs a GPU node -> min_needed=1.
        # surplus=1. n_cpu is first candidate (0 util, 0 GPU -> sorts first).
        # n_cpu is empty -> tainted (no pods to check safety for).
        # n_gpu cannot be tainted because its GPU pod can't fit on n_cpu.
        assert "n-gpu" not in to_taint


# ============================================================================
# GPU fleet capacity tests
# ============================================================================


class TestGPUFleetCapacity:
    """Fleet-aware GPU tests."""

    def test_gpu_fleet_mixed_instances(self):
        """Fleet with heterogeneous GPU nodes (1-GPU and 8-GPU), mixed
        GPU workloads -> correct bin-packing and surplus calculation."""
        cfg = make_config(min_nodes=1, spare_capacity_nodes=0, spare_capacity_ratio=0.0)
        n_big = make_fleet_node("n-big", nodepool="gpu-8xl", fleet="gpu", cpu=96.0, gpu=8)
        n_small = make_fleet_node("n-small", nodepool="gpu-1xl", fleet="gpu", cpu=16.0, gpu=1)
        # Place a 4-GPU pod on the big node
        n_big.pods = [make_pod("p1", cpu=4.0, mem=8 * GiB, gpu=4, node_name="n-big")]

        nodes = {"n-big": n_big, "n-small": n_small}

        to_taint, _to_untaint, _mandatory, _rate_limited = compute_taints(nodes, cfg, group_key=_fleet_group_key)

        # bin_pack: 1 pod (4 GPU, 4 CPU). Sorted by (gpu, cpu) desc: p1(4,4).
        # Bins sorted by cpu desc: n_big(96), n_small(16).
        # p1 fits on n_big (8 GPU). min_needed=1. surplus=1.
        # n_small tainted (lower cpu, lower gpu).
        assert "n-small" in to_taint
        assert "n-big" not in to_taint

    def test_gpu_fleet_spare_capacity(self):
        """Fleet with GPU nodes: spare capacity threshold includes GPU
        utilization when checking if a node is low-utilization.

        GPU utilization factors into the max(cpu, mem, gpu) utilization,
        so a node with high GPU usage is NOT a spare node.
        """
        cfg = make_config(
            min_nodes=1,
            spare_capacity_nodes=1,
            spare_capacity_ratio=0.0,
            spare_capacity_threshold=0.4,
        )
        # 3 nodes in same fleet: one heavily used, two idle
        n_busy = make_fleet_node("n-busy", nodepool="gpu-8", fleet="gpu", cpu=96.0, gpu=8)
        n_busy.pods = [make_pod("p1", cpu=80.0, mem=8 * GiB, gpu=7, node_name="n-busy")]
        n_idle1 = make_fleet_node("n-idle1", nodepool="gpu-8", fleet="gpu", cpu=96.0, gpu=8)
        n_idle2 = make_fleet_node("n-idle2", nodepool="gpu-8", fleet="gpu", cpu=96.0, gpu=8)

        nodes = {"n-busy": n_busy, "n-idle1": n_idle1, "n-idle2": n_idle2}

        to_taint, _to_untaint, _mandatory, _rate_limited = compute_taints(nodes, cfg, group_key=_fleet_group_key)

        # bin_pack: 1 pod -> min_needed=1. surplus=2.
        # Taint priority (same cpu/gpu, by utilization): n-idle1(0%), n-idle2(0%), n-busy(87.5%).
        # Candidate n-idle1: spare_after = {n-idle2} = 1 >= 1 required -> tainted.
        # Candidate n-idle2: spare_after = 0 < 1 required -> blocked (spare capacity).
        # Candidate n-busy: spare_after = {n-idle2} = 1 >= 1 (n-busy is >0.4 so not spare) -> tainted.
        # Result: n-idle1 and n-busy tainted, n-idle2 preserved as spare.
        assert len(to_taint) == 2
        assert "n-idle1" in to_taint
        assert "n-busy" in to_taint
        assert "n-idle2" not in to_taint


# ============================================================================
# Fleet cooldown tests (cross-NodePool)
# ============================================================================


class TestFleetCooldownCrossPool:
    """Tests for fleet cooldown spanning multiple NodePools."""

    @patch("compactor.pathlib.Path.touch")
    @patch("compactor.remove_taint")
    @patch("compactor.apply_taint")
    @patch("compactor.compute_taints")
    @patch("compactor.check_pending_pods")
    @patch("compactor.build_node_states")
    @patch("compactor.discover_managed_nodes")
    def test_fleet_cooldown_spans_nodepools(
        self,
        mock_discover,
        mock_build,
        mock_check,
        mock_compute,
        mock_apply,
        mock_remove,
        mock_touch,
    ):
        """Burst untaint on NodePool m8g-48xlarge should block taint on a node
        in NodePool m8g-8xlarge when both are in the same fleet 'm8g'.

        The fleet cooldown key is the fleet name (or nodepool if no fleet),
        so all NodePools sharing a fleet share the cooldown.
        """
        client = MagicMock()
        cfg = make_config(fleet_cooldown=900)
        taint_times: dict[str, float] = {}
        fleet_cooldown_times: dict[str, float] = {}

        # Iteration 1: burst untaint on n-big (nodepool m8g-48xlarge)
        n_big = make_fleet_node("n-big", nodepool="m8g-48xlarge", fleet="m8g", cpu=192.0, is_tainted=True)
        n_small = make_fleet_node("n-small", nodepool="m8g-8xlarge", fleet="m8g", cpu=16.0)
        node_states = {"n-big": n_big, "n-small": n_small}

        mock_discover.return_value = {"n-big": "m8g-48xlarge", "n-small": "m8g-8xlarge"}
        mock_build.return_value = (node_states, [])
        mock_check.return_value = ({"n-big"}, 1)  # burst untaint for n-big
        mock_compute.return_value = (set(), set(), set(), set())

        reconcile(client, cfg, taint_times, fleet_cooldown_times)

        # Fleet cooldown should be recorded for the fleet "m8g"
        assert "m8g" in fleet_cooldown_times

        # Iteration 2: compactor wants to taint n-small (different NodePool,
        # same fleet). Fleet cooldown is keyed on fleet (not nodepool),
        # so this should be blocked. Both NodePools share the fleet "m8g",
        # fleet-aware cooldown lands.
        mock_discover.reset_mock()
        mock_build.reset_mock()
        mock_check.reset_mock()
        mock_compute.reset_mock()
        mock_apply.reset_mock()
        mock_remove.reset_mock()

        n_big_2 = make_fleet_node("n-big", nodepool="m8g-48xlarge", fleet="m8g", cpu=192.0)
        n_small_2 = make_fleet_node("n-small", nodepool="m8g-8xlarge", fleet="m8g", cpu=16.0)
        node_states_2 = {"n-big": n_big_2, "n-small": n_small_2}

        mock_discover.return_value = {"n-big": "m8g-48xlarge", "n-small": "m8g-8xlarge"}
        mock_build.return_value = (node_states_2, [])
        mock_check.return_value = (set(), 0)
        mock_compute.return_value = ({"n-small"}, set(), set(), set())

        reconcile(client, cfg, taint_times, fleet_cooldown_times)

        # n-small should NOT be tainted because the fleet's cooldown is active
        # (burst happened on sibling NodePool m8g-48xlarge)
        apply_calls = [c[0][1] for c in mock_apply.call_args_list]
        assert "n-small" not in apply_calls

    @patch("compactor.pathlib.Path.touch")
    @patch("compactor.remove_taint")
    @patch("compactor.apply_taint")
    @patch("compactor.compute_taints")
    @patch("compactor.check_pending_pods")
    @patch("compactor.build_node_states")
    @patch("compactor.discover_managed_nodes")
    def test_fleet_cooldown_does_not_span_fleets(
        self,
        mock_discover,
        mock_build,
        mock_check,
        mock_compute,
        mock_apply,
        mock_remove,
        mock_touch,
    ):
        """Burst on fleet 'm8g' should NOT block fleet 'r7a'.

        Fleet cooldown is per-fleet, so different fleets are independent.
        """
        client = MagicMock()
        cfg = make_config(fleet_cooldown=900)
        taint_times: dict[str, float] = {}
        # Only fleet "m8g" has active cooldown
        fleet_cooldown_times: dict[str, float] = {"m8g": time.time()}

        n_m8g = make_fleet_node("n-m8g", nodepool="m8g-48xlarge", fleet="m8g", cpu=192.0)
        n_r7a = make_fleet_node("n-r7a", nodepool="r7a-4xlarge", fleet="r7a", cpu=192.0)
        node_states = {"n-m8g": n_m8g, "n-r7a": n_r7a}

        mock_discover.return_value = {"n-m8g": "m8g-48xlarge", "n-r7a": "r7a-4xlarge"}
        mock_build.return_value = (node_states, [])
        mock_check.return_value = (set(), 0)
        mock_compute.return_value = ({"n-m8g", "n-r7a"}, set(), set(), set())

        reconcile(client, cfg, taint_times, fleet_cooldown_times)

        # n-r7a (fleet "r7a") should be tainted -- no cooldown for that fleet
        apply_calls = [c[0][1] for c in mock_apply.call_args_list]
        assert "n-r7a" in apply_calls
        # n-m8g should be blocked by its fleet's cooldown
        assert "n-m8g" not in apply_calls

    @patch("compactor.pathlib.Path.touch")
    @patch("compactor.remove_taint")
    @patch("compactor.apply_taint")
    @patch("compactor.compute_taints")
    @patch("compactor.check_pending_pods")
    @patch("compactor.build_node_states")
    @patch("compactor.discover_managed_nodes")
    def test_fleet_cooldown_surplus_uses_full_fleet(
        self,
        mock_discover,
        mock_build,
        mock_check,
        mock_compute,
        mock_apply,
        mock_remove,
        mock_touch,
    ):
        """Surplus override counts all fleet nodes, not just one NodePool.

        4 nodes across 2 NodePools in fleet "m8g". 3 tainted + 1 to-taint = 4/4.
        >50% surplus -> cooldown halved (450s). With 500s elapsed, taint proceeds.
        """
        client = MagicMock()
        cfg = make_config(fleet_cooldown=900)
        taint_times: dict[str, float] = {}
        # Fleet cooldown was 500 seconds ago: past half (450s) but within full (900s)
        fleet_cooldown_times: dict[str, float] = {"m8g": time.time() - 500}

        # 4 nodes across 2 NodePools, all fleet "m8g"
        n1 = make_fleet_node("n1", nodepool="m8g-48xlarge", fleet="m8g", cpu=192.0, is_tainted=True)
        n2 = make_fleet_node("n2", nodepool="m8g-48xlarge", fleet="m8g", cpu=192.0, is_tainted=True)
        n3 = make_fleet_node("n3", nodepool="m8g-8xlarge", fleet="m8g", cpu=16.0, is_tainted=True)
        n4 = make_fleet_node("n4", nodepool="m8g-8xlarge", fleet="m8g", cpu=16.0, is_tainted=False)
        node_states = {"n1": n1, "n2": n2, "n3": n3, "n4": n4}

        mock_discover.return_value = {
            "n1": "m8g-48xlarge",
            "n2": "m8g-48xlarge",
            "n3": "m8g-8xlarge",
            "n4": "m8g-8xlarge",
        }
        mock_build.return_value = (node_states, [])
        mock_check.return_value = (set(), 0)
        mock_compute.return_value = ({"n4"}, set(), set(), set())

        reconcile(client, cfg, taint_times, fleet_cooldown_times)

        # With >50% surplus across the full fleet, effective cooldown = 450s.
        # 500s elapsed > 450s -> taint should proceed.
        mock_apply.assert_called_once_with(client, "n4", cfg.taint_key, cfg.dry_run)


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
        mock_check.return_value = (set(), 0)
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
        mock_check.return_value = ({"n1"}, 1)
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
        mock_check.return_value = (set(), 0)  # no burst untaint
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
        mock_check.return_value = (set(), 0)  # no burst untaint
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
        mock_check.return_value = (set(), 0)
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
        mock_check.return_value = (set(), 0)
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
        mock_check.return_value = (set(), 0)
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
        mock_check.return_value = (set(), 0)
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
        mock_check.return_value = (set(), 0)
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
        mock_check.return_value = (set(), 0)
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
        mock_check.return_value = (set(), 0)
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
        mock_check.return_value = (set(), 0)
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
        mock_check.return_value = (set(), 0)
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
        mock_check.return_value = (set(), 0)
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
        mock_check.return_value = ({"n1"}, 1)  # burst wants untaint
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
        mock_check.return_value = (set(), 0)
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
        mock_check.return_value = (set(), 0)
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
        mock_check.return_value = (set(), 0)
        # Return n2, n3 as rate-limited
        mock_compute.return_value = ({"n1"}, set(), set(), {"n2", "n3"})

        reconcile(client, cfg, {})

        # Verify per-pool rate-limit metrics were emitted
        mock_rate_limit_blocks.labels.assert_called()
        pool_labels = {c[1]["nodepool"] for c in mock_rate_limit_blocks.labels.call_args_list}
        assert pool_labels == {"pool-a", "pool-b"}

    @patch("compactor.pathlib.Path.touch")
    @patch("compactor.compute_taints")
    @patch("compactor.check_pending_pods")
    @patch("compactor.build_node_states")
    @patch("compactor.discover_managed_nodes")
    def test_reconcile_threads_peak_history(
        self,
        mock_discover,
        mock_build,
        mock_check,
        mock_compute,
        mock_touch,
    ):
        """reconcile() forwards the caller's peak_history dict to compute_taints."""
        client = MagicMock()
        cfg = make_config()

        n1 = make_node("n1")
        mock_discover.return_value = {"n1": "pool"}
        mock_build.return_value = ({"n1": n1}, [])
        mock_check.return_value = (set(), 0)
        mock_compute.return_value = (set(), set(), set(), set())

        peak_history: dict[str, list[tuple[float, int]]] = {}
        reconcile(client, cfg, {}, {}, peak_history)

        assert mock_compute.call_args.kwargs["peak_history"] is peak_history

    @patch("compactor.m.pending_pods_compatible")
    @patch("compactor.apply_pending_phantom_load")
    @patch("compactor.pathlib.Path.touch")
    @patch("compactor.compute_taints")
    @patch("compactor.check_pending_pods")
    @patch("compactor.build_node_states")
    @patch("compactor.discover_managed_nodes")
    def test_reconcile_pending_pods_compatible_uses_check_return(
        self,
        mock_discover,
        mock_build,
        mock_check,
        mock_compute,
        mock_touch,
        mock_phantom,
        mock_gauge,
    ):
        """pending_pods_compatible gauge reflects check_pending_pods's count, not len(pending_pods)."""
        client = MagicMock()
        cfg = make_config()

        n1 = make_node("n1")
        # 10 pending pods, but only 5 are compatible with tainted nodes
        pending_pods = [MagicMock() for _ in range(10)]
        mock_discover.return_value = {"n1": "pool"}
        mock_build.return_value = ({"n1": n1}, pending_pods)
        mock_check.return_value = ({"n1"}, 5)
        mock_compute.return_value = (set(), set(), set(), set())

        reconcile(client, cfg, {})

        mock_gauge.set.assert_called_once_with(5)


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
        mock_check.return_value = ({"n1"}, 1)  # burst untaint for n1
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
        mock_check.return_value = (set(), 0)  # no burst this time
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
        mock_check.return_value = (set(), 0)
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
        mock_check.return_value = (set(), 0)
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
        mock_check.return_value = (set(), 0)
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
        mock_check.return_value = (set(), 0)
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
        mock_check.return_value = (set(), 0)
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

        def reconcile_side_effect(*args, **kwargs):
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


# ============================================================================
# Peak window tests
# ============================================================================


def make_pending_pod_mock(
    cpu: str = "1",
    memory: str = "1Gi",
    gpu: int | None = None,
    age_seconds: float = 60.0,
    name: str = "pending-pod",
    namespace: str = "default",
    tolerations=None,
    node_selector=None,
):
    """Build a MagicMock lightkube Pod for pending-pod tests."""
    pod = MagicMock()
    pod.metadata.name = name
    pod.metadata.namespace = namespace
    pod.metadata.creationTimestamp = datetime.now(UTC) - timedelta(seconds=age_seconds)
    pod.spec.tolerations = tolerations
    pod.spec.nodeSelector = node_selector
    pod.spec.affinity = None
    container = MagicMock()
    requests = {"cpu": cpu, "memory": memory}
    if gpu is not None:
        requests["nvidia.com/gpu"] = gpu
    container.resources.requests = requests
    pod.spec.containers = [container]
    return pod


class TestPeakWindow:
    """Tests for the per-fleet sliding-window peak tracker in compute_taints."""

    def test_empty_peak_history_uses_current_bin_pack(self):
        """First tick: empty history → min_needed is bin_pack(current pods)."""
        cfg = make_config(min_nodes=1, taint_rate=1.0)
        n1 = make_node("n1")
        n1.pods = [make_pod("p1", cpu=8.0, node_name="n1")]
        n2 = make_node("n2")
        n2.pods = [make_pod("p2", cpu=8.0, node_name="n2")]
        n3 = make_node("n3")
        nodes = {"n1": n1, "n2": n2, "n3": n3}
        peak_history: dict[str, list[tuple[float, int]]] = {}

        to_taint, *_ = compute_taints(nodes, cfg, peak_history=peak_history)

        # bin_pack: 2 pods of 8 CPU each fit on 1 node of 16 CPU → min=1
        # surplus = 3 - 1 = 2 → 2 nodes tainted
        assert len(to_taint) == 2
        assert "default" in peak_history
        assert len(peak_history["default"]) == 1

    def test_history_accumulates_max_wins(self):
        """Second tick appends; peak_min is the max of all entries in window."""
        cfg = make_config(min_nodes=1, taint_rate=1.0)
        nodes = {f"n{i}": make_node(f"n{i}") for i in range(5)}
        peak_history: dict[str, list[tuple[float, int]]] = {
            "default": [(time.monotonic() - 10, 4)],
        }

        to_taint, *_ = compute_taints(nodes, cfg, peak_history=peak_history)

        # peak_min = max(4, 0) = 4; min_needed = max(4,1) = 4; surplus=1
        assert len(to_taint) == 1
        assert len(peak_history["default"]) == 2
        assert max(v for _, v in peak_history["default"]) == 4

    def test_history_prunes_by_age(self):
        """Entries older than PEAK_WINDOW_SECONDS are dropped."""
        cfg = make_config(min_nodes=1, taint_rate=1.0)
        nodes = {f"n{i}": make_node(f"n{i}") for i in range(5)}
        now = time.monotonic()
        peak_history: dict[str, list[tuple[float, int]]] = {
            "default": [
                (now - PEAK_WINDOW_SECONDS - 100, 4),
                (now - 10, 1),
            ],
        }

        compute_taints(nodes, cfg, peak_history=peak_history)

        timestamps = [t for t, _ in peak_history["default"]]
        assert all(t >= now - PEAK_WINDOW_SECONDS - 1 for t in timestamps)
        assert max(v for _, v in peak_history["default"]) == 1

    def test_history_capped_to_max_entries(self):
        """Inserting 10000 fake entries: post-call list length is bounded."""
        cfg = make_config(min_nodes=1, interval=20, taint_rate=1.0)
        nodes = {"n1": make_node("n1")}
        now = time.monotonic()
        peak_history: dict[str, list[tuple[float, int]]] = {
            "default": [(now - i * 0.0001, 0) for i in range(10000)],
        }

        compute_taints(nodes, cfg, peak_history=peak_history)

        expected_cap = max(64, PEAK_WINDOW_SECONDS // max(1, cfg.interval) + 60)
        assert len(peak_history["default"]) <= expected_cap

    def test_fleet_rename_stale_key_pruned(self):
        """Key not present in current groups (with only-stale entries) is dropped."""
        cfg = make_config(min_nodes=1, taint_rate=1.0)
        nodes = {"n1": make_node("n1", nodepool="pool-a")}
        now = time.monotonic()
        peak_history: dict[str, list[tuple[float, int]]] = {
            "renamed-pool": [(now - PEAK_WINDOW_SECONDS - 5, 99)],
        }

        compute_taints(nodes, cfg, peak_history=peak_history)

        assert "renamed-pool" not in peak_history
        assert "pool-a" in peak_history

    def test_drop_out_cliff_when_old_peak_expires(self):
        """Old peak=20 sample expires this tick → peak_min becomes current."""
        cfg = make_config(min_nodes=1, taint_rate=1.0)
        nodes = {f"n{i}": make_node(f"n{i}") for i in range(5)}
        now = time.monotonic()
        peak_history: dict[str, list[tuple[float, int]]] = {
            "default": [(now - PEAK_WINDOW_SECONDS - 1, 20)],
        }

        to_taint, *_ = compute_taints(nodes, cfg, peak_history=peak_history)

        # Stale entry pruned at top; current=0 → min=max(0,1)=1; surplus=4
        assert len(to_taint) == 4

    def test_peak_min_beats_min_nodes(self):
        """cfg.min_nodes=3, peak history says 10 → min_needed=10."""
        cfg = make_config(min_nodes=3, taint_rate=1.0)
        nodes = {f"n{i}": make_node(f"n{i}") for i in range(15)}
        now = time.monotonic()
        peak_history: dict[str, list[tuple[float, int]]] = {
            "default": [(now - 5, 10)],
        }

        to_taint, *_ = compute_taints(nodes, cfg, peak_history=peak_history)

        assert len(to_taint) == 5

    def test_peak_min_loses_to_min_nodes(self):
        """cfg.min_nodes=10 dominates a smaller peak history."""
        cfg = make_config(min_nodes=10, taint_rate=1.0)
        nodes = {f"n{i}": make_node(f"n{i}") for i in range(12)}
        now = time.monotonic()
        peak_history: dict[str, list[tuple[float, int]]] = {
            "default": [(now - 30, 3), (now - 20, 4), (now - 10, 5)],
        }

        to_taint, *_ = compute_taints(nodes, cfg, peak_history=peak_history)

        assert len(to_taint) == 2

    def test_none_peak_history_preserves_legacy_behavior(self):
        """peak_history=None disables the floor entirely; behavior matches today."""
        cfg = make_config(min_nodes=1, taint_rate=1.0)
        n1 = make_node("n1")
        n1.pods = [make_pod("p1", cpu=8.0, node_name="n1")]
        nodes = {"n1": n1, "n2": make_node("n2"), "n3": make_node("n3")}

        to_taint_a, *_ = compute_taints(nodes, cfg)
        to_taint_b, *_ = compute_taints(nodes, cfg, peak_history=None)

        assert to_taint_a == to_taint_b


# ============================================================================
# Pending pods in bin-pack tests
# ============================================================================


class TestPendingPodsInBinPack:
    """Tests for pending-pod inclusion in compute_taints bin-pack input."""

    def test_no_pending_pods_none_behaves_legacy(self):
        """pending_pods=None and [] behave identically to today."""
        cfg = make_config(min_nodes=1, taint_rate=1.0)
        n1 = make_node("n1")
        n1.pods = [make_pod("p1", cpu=8.0, node_name="n1")]
        nodes = {"n1": n1, "n2": make_node("n2"), "n3": make_node("n3")}

        to_taint_a, *_ = compute_taints(nodes, cfg)
        to_taint_b, *_ = compute_taints(nodes, cfg, pending_pods=None)
        to_taint_c, *_ = compute_taints(nodes, cfg, pending_pods=[])

        assert to_taint_a == to_taint_b == to_taint_c

    def test_pending_pod_matching_is_included(self):
        """Pending pods that fit and match are added to bin-pack input."""
        cfg = make_config(min_nodes=1, taint_rate=1.0)
        nodes = {f"n{i}": make_node(f"n{i}") for i in range(3)}

        pp = make_pending_pod_mock(cpu="16", memory="1Gi")
        pp2 = make_pending_pod_mock(cpu="16", memory="1Gi", name="pp2")

        # 1 pending pod fills 1 node → bin_pack=1; surplus = 3-1 = 2 tainted
        to_taint, *_ = compute_taints(nodes, cfg, pending_pods=[pp])
        # 2 pending pods fill 2 nodes → bin_pack=2; surplus = 3-2 = 1 tainted
        to_taint2, *_ = compute_taints(nodes, cfg, pending_pods=[pp, pp2])

        assert len(to_taint) == 2
        assert len(to_taint2) == 1

    def test_pending_pod_not_matching_nodeselector_is_excluded(self):
        """Pending pod with mismatched nodeSelector is excluded from bin-pack."""
        cfg = make_config(min_nodes=1, taint_rate=1.0)
        n = make_node("n1")
        n.labels = {"role": "build"}
        nodes = {"n1": n, "n2": make_node("n2"), "n3": make_node("n3")}

        pp_wrong = make_pending_pod_mock(cpu="16", node_selector={"role": "gpu"})

        filtered = pending_pods_for_group([pp_wrong], list(nodes.values()), cfg.taint_key)
        assert filtered == []

        to_taint, *_ = compute_taints(nodes, cfg, pending_pods=[pp_wrong])
        # bin_pack returned 0 → min=max(0,1)=1; surplus=2 tainted
        assert len(to_taint) == 2

    def test_pending_pod_with_zero_requests_included_no_inflation(self):
        """No-request pending pod is included but doesn't push min_needed up."""
        cfg = make_config(min_nodes=1, taint_rate=1.0)
        nodes = {f"n{i}": make_node(f"n{i}") for i in range(3)}

        pp = make_pending_pod_mock(cpu="0", memory="0")

        filtered = pending_pods_for_group([pp], list(nodes.values()), cfg.taint_key)
        assert len(filtered) == 1
        assert filtered[0].cpu_request == 0
        assert filtered[0].memory_request == 0

        to_taint, *_ = compute_taints(nodes, cfg, pending_pods=[pp])
        # Zero-request pod doesn't inflate bin_pack; surplus = 3-1 = 2 tainted
        assert len(to_taint) == 2
