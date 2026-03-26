"""Tests for phantom.py — phantom load simulation for pending pods."""

import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

from models import Config, NodeState, PodInfo
from phantom import (
    PHANTOM_MAX_PENDING_SECONDS,
    PHANTOM_MIN_PENDING_SECONDS,
    apply_pending_phantom_load,
)

GiB = 1024**3


def make_config(**overrides):
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
        "taint_rate": 0.3,
        "spare_capacity_nodes": 3,
        "spare_capacity_ratio": 0.15,
        "spare_capacity_threshold": 0.4,
        "capacity_reservation_nodes": 0,
    }
    defaults.update(overrides)
    return Config(**defaults)


def make_node_state(
    name="node-1",
    nodepool="pool-1",
    is_tainted=False,
    node_taints=None,
    allocatable_cpu=8.0,
    allocatable_memory=32 * GiB,
    pods=None,
    labels=None,
):
    return NodeState(
        name=name,
        nodepool=nodepool,
        allocatable_cpu=allocatable_cpu,
        allocatable_memory=allocatable_memory,
        creation_time=datetime.now(UTC) - timedelta(hours=24),
        pods=pods or [],
        is_tainted=is_tainted,
        node_taints=node_taints or [],
        labels=labels or {},
    )


def make_pending_pod(name="pending-1", namespace="default", cpu="1", memory="1Gi", age_seconds=60):
    """Create a mock pending pod (raw API object, not PodInfo)."""
    pod = MagicMock()
    pod.metadata.name = name
    pod.metadata.namespace = namespace
    pod.metadata.creationTimestamp = datetime.now(UTC) - timedelta(seconds=age_seconds)
    pod.spec.tolerations = []
    pod.spec.nodeSelector = None
    pod.spec.affinity = None

    container = MagicMock()
    container.resources.requests = {"cpu": cpu, "memory": memory}
    pod.spec.containers = [container]
    return pod


# ============================================================================
# Basic placement tests
# ============================================================================


class TestPhantomPlacementBasic(unittest.TestCase):
    def setUp(self):
        self.cfg = make_config()

    def test_empty_pending_list_no_changes(self):
        """No pending pods = no phantom pods added."""
        node = make_node_state()
        states = {"node-1": node}
        apply_pending_phantom_load(states, [], self.cfg)
        self.assertEqual(len(node.pods), 0)

    def test_empty_node_states_no_crash(self):
        """Empty node_states dict should not crash."""
        pod = make_pending_pod()
        apply_pending_phantom_load({}, [pod], self.cfg)

    def test_pending_pod_placed_on_compatible_node(self):
        """A compatible pending pod gets placed as phantom load."""
        node = make_node_state(allocatable_cpu=8.0, allocatable_memory=32 * GiB)
        states = {"node-1": node}
        pod = make_pending_pod(cpu="1", memory="1Gi", age_seconds=60)

        apply_pending_phantom_load(states, [pod], self.cfg)

        phantom_pods = [p for p in node.pods if p.is_phantom]
        self.assertEqual(len(phantom_pods), 1)
        self.assertEqual(phantom_pods[0].cpu_request, 1.0)
        self.assertTrue(phantom_pods[0].is_phantom)
        self.assertFalse(phantom_pods[0].is_daemonset)


# ============================================================================
# Least-utilized node selection
# ============================================================================


class TestPhantomLeastUtilized(unittest.TestCase):
    def setUp(self):
        self.cfg = make_config()

    def test_placed_on_least_utilized_node(self):
        """Pending pod should land on the node with lowest utilization."""
        # node-a: 50% utilized (4/8 CPU)
        node_a = make_node_state(
            name="node-a",
            allocatable_cpu=8.0,
            allocatable_memory=32 * GiB,
            pods=[PodInfo("w1", "ns", 4.0, 1 * GiB, "node-a", False, datetime.now(UTC))],
        )
        # node-b: 12.5% utilized (1/8 CPU)
        node_b = make_node_state(
            name="node-b",
            allocatable_cpu=8.0,
            allocatable_memory=32 * GiB,
            pods=[PodInfo("w2", "ns", 1.0, 1 * GiB, "node-b", False, datetime.now(UTC))],
        )
        states = {"node-a": node_a, "node-b": node_b}
        pod = make_pending_pod(cpu="1", memory="1Gi", age_seconds=60)

        apply_pending_phantom_load(states, [pod], self.cfg)

        # Should be placed on node-b (lower utilization)
        phantom_a = [p for p in node_a.pods if p.is_phantom]
        phantom_b = [p for p in node_b.pods if p.is_phantom]
        self.assertEqual(len(phantom_a), 0)
        self.assertEqual(len(phantom_b), 1)


# ============================================================================
# Tainted node exclusion
# ============================================================================


class TestPhantomTaintedNodes(unittest.TestCase):
    def setUp(self):
        self.cfg = make_config()

    def test_tainted_nodes_not_eligible(self):
        """Tainted nodes should NOT receive phantom pods."""
        node = make_node_state(
            name="tainted-node",
            is_tainted=True,
            allocatable_cpu=8.0,
            allocatable_memory=32 * GiB,
        )
        states = {"tainted-node": node}
        pod = make_pending_pod(cpu="1", memory="1Gi", age_seconds=60)

        apply_pending_phantom_load(states, [pod], self.cfg)

        self.assertEqual(len(node.pods), 0)

    def test_only_untainted_nodes_receive_phantom(self):
        """With mixed tainted/untainted, only untainted gets phantom."""
        tainted = make_node_state(name="tainted", is_tainted=True)
        untainted = make_node_state(name="untainted", is_tainted=False)
        states = {"tainted": tainted, "untainted": untainted}
        pod = make_pending_pod(cpu="1", memory="1Gi", age_seconds=60)

        apply_pending_phantom_load(states, [pod], self.cfg)

        self.assertEqual(len(tainted.pods), 0)
        self.assertEqual(len([p for p in untainted.pods if p.is_phantom]), 1)


# ============================================================================
# Age filtering
# ============================================================================


class TestPhantomAgeFilters(unittest.TestCase):
    def setUp(self):
        self.cfg = make_config()

    def test_stale_pod_skipped(self):
        """Pod pending > 120s should be skipped (stale)."""
        node = make_node_state()
        states = {"node-1": node}
        pod = make_pending_pod(age_seconds=PHANTOM_MAX_PENDING_SECONDS + 10)

        apply_pending_phantom_load(states, [pod], self.cfg)

        self.assertEqual(len(node.pods), 0)

    def test_too_young_pod_skipped(self):
        """Pod pending < 30s should be skipped."""
        node = make_node_state()
        states = {"node-1": node}
        pod = make_pending_pod(age_seconds=PHANTOM_MIN_PENDING_SECONDS - 5)

        apply_pending_phantom_load(states, [pod], self.cfg)

        self.assertEqual(len(node.pods), 0)

    def test_pod_at_boundary_accepted(self):
        """Pod at exactly 30s should be accepted."""
        node = make_node_state()
        states = {"node-1": node}
        pod = make_pending_pod(age_seconds=PHANTOM_MIN_PENDING_SECONDS)

        apply_pending_phantom_load(states, [pod], self.cfg)

        # age == 30s means (now - creationTimestamp).total_seconds() == 30,
        # which is NOT < 30 so it passes the filter
        self.assertEqual(len([p for p in node.pods if p.is_phantom]), 1)

    def test_pod_just_under_max_accepted(self):
        """Pod at 119s (just under max) should be accepted."""
        node = make_node_state()
        states = {"node-1": node}
        pod = make_pending_pod(age_seconds=PHANTOM_MAX_PENDING_SECONDS - 1)

        apply_pending_phantom_load(states, [pod], self.cfg)

        self.assertEqual(len([p for p in node.pods if p.is_phantom]), 1)


# ============================================================================
# Phantom load cap (30%)
# ============================================================================


class TestPhantomLoadCap(unittest.TestCase):
    def setUp(self):
        self.cfg = make_config()

    def test_pod_exceeding_cpu_cap_skipped(self):
        """Pod that would push phantom CPU above 30% of allocatable is skipped."""
        # Node with 10 CPU allocatable. 30% cap = 3 CPU phantom max.
        node = make_node_state(name="node-1", allocatable_cpu=10.0, allocatable_memory=100 * GiB)
        states = {"node-1": node}

        # First pod: 2 CPU (20% phantom) — should be placed
        pod1 = make_pending_pod(name="p1", cpu="2", memory="1Gi", age_seconds=60)
        # Second pod: 2 CPU (would make 40% phantom) — should be skipped
        pod2 = make_pending_pod(name="p2", cpu="2", memory="1Gi", age_seconds=60)

        apply_pending_phantom_load(states, [pod1, pod2], self.cfg)

        phantom_pods = [p for p in node.pods if p.is_phantom]
        self.assertEqual(len(phantom_pods), 1)
        self.assertIn("p1", phantom_pods[0].name)

    def test_pod_exceeding_memory_cap_skipped(self):
        """Pod that would push phantom memory above 30% of allocatable is skipped."""
        # Node with 32 GiB. 30% cap = ~9.6 GiB phantom max.
        node = make_node_state(name="node-1", allocatable_cpu=100.0, allocatable_memory=32 * GiB)
        states = {"node-1": node}

        # First pod: 8 GiB (25% phantom) — should be placed
        pod1 = make_pending_pod(name="p1", cpu="1", memory="8Gi", age_seconds=60)
        # Second pod: 8 GiB (would make 50% phantom) — should be skipped
        pod2 = make_pending_pod(name="p2", cpu="1", memory="8Gi", age_seconds=60)

        apply_pending_phantom_load(states, [pod1, pod2], self.cfg)

        phantom_pods = [p for p in node.pods if p.is_phantom]
        self.assertEqual(len(phantom_pods), 1)
        self.assertIn("p1", phantom_pods[0].name)

    def test_cap_uses_only_phantom_pods(self):
        """The 30% cap is computed from phantom pods only, not real pods."""
        # Node: 10 CPU, 5 CPU already used by real pods (50% real utilization).
        # Phantom cap is still 30% of 10 = 3 CPU.
        node = make_node_state(
            name="node-1",
            allocatable_cpu=10.0,
            allocatable_memory=100 * GiB,
            pods=[PodInfo("real-1", "ns", 5.0, 1 * GiB, "node-1", False, datetime.now(UTC))],
        )
        states = {"node-1": node}

        # 2 CPU phantom (20% of allocatable) — should be placed despite 50% real util
        pod = make_pending_pod(cpu="2", memory="1Gi", age_seconds=60)
        apply_pending_phantom_load(states, [pod], self.cfg)

        phantom_pods = [p for p in node.pods if p.is_phantom]
        self.assertEqual(len(phantom_pods), 1)


# ============================================================================
# Incompatible pods
# ============================================================================


class TestPhantomIncompatiblePods(unittest.TestCase):
    def setUp(self):
        self.cfg = make_config()

    def test_pod_incompatible_with_all_nodes_skipped(self):
        """Pod that doesn't match any untainted node is skipped."""
        # Node has a taint the pod can't tolerate
        taint = MagicMock()
        taint.key = "special-taint"
        taint.value = "yes"
        taint.effect = "NoSchedule"

        node = make_node_state(
            name="node-1",
            node_taints=[taint],
            allocatable_cpu=8.0,
            allocatable_memory=32 * GiB,
        )
        states = {"node-1": node}

        # Pod has no tolerations
        pod = make_pending_pod(cpu="1", memory="1Gi", age_seconds=60)
        pod.spec.tolerations = []

        apply_pending_phantom_load(states, [pod], self.cfg)

        self.assertEqual(len(node.pods), 0)

    def test_pod_with_node_selector_mismatch(self):
        """Pod with nodeSelector that doesn't match node labels is skipped."""
        node = make_node_state(labels={"tier": "cpu"})
        states = {"node-1": node}

        pod = make_pending_pod(cpu="1", memory="1Gi", age_seconds=60)
        pod.spec.nodeSelector = {"tier": "gpu"}

        apply_pending_phantom_load(states, [pod], self.cfg)

        self.assertEqual(len(node.pods), 0)


# ============================================================================
# Multiple phantom pods accumulation
# ============================================================================


class TestPhantomAccumulation(unittest.TestCase):
    def setUp(self):
        self.cfg = make_config()

    def test_multiple_phantoms_accumulate(self):
        """Multiple phantom pods can be placed and accumulate correctly."""
        node = make_node_state(
            name="node-1",
            allocatable_cpu=16.0,
            allocatable_memory=64 * GiB,
        )
        states = {"node-1": node}

        pods = [make_pending_pod(name=f"p{i}", cpu="1", memory="1Gi", age_seconds=60) for i in range(4)]

        apply_pending_phantom_load(states, pods, self.cfg)

        phantom_pods = [p for p in node.pods if p.is_phantom]
        # 4 pods * 1 CPU each = 4 CPU = 25% of 16 CPU (under 30% cap)
        self.assertEqual(len(phantom_pods), 4)

    def test_phantoms_affect_utilization(self):
        """Phantom pods affect cpu_used, memory_used, and utilization properties."""
        node = make_node_state(
            name="node-1",
            allocatable_cpu=10.0,
            allocatable_memory=40 * GiB,
        )
        states = {"node-1": node}

        self.assertAlmostEqual(node.utilization, 0.0)

        pod = make_pending_pod(cpu="2", memory="4Gi", age_seconds=60)
        apply_pending_phantom_load(states, [pod], self.cfg)

        # Phantom pod contributes to workload_pods (non-daemonset)
        self.assertAlmostEqual(node.cpu_used, 2.0)
        self.assertEqual(node.memory_used, 4 * GiB)
        self.assertGreater(node.utilization, 0.0)

    def test_phantoms_spread_across_nodes(self):
        """Multiple phantoms should spread via LeastAllocated."""
        node_a = make_node_state(name="node-a", allocatable_cpu=8.0, allocatable_memory=32 * GiB)
        node_b = make_node_state(name="node-b", allocatable_cpu=8.0, allocatable_memory=32 * GiB)
        states = {"node-a": node_a, "node-b": node_b}

        # First pod goes to one of them (both at 0% util, either is fine)
        # Second pod should go to the other (now the first has higher util)
        pods = [
            make_pending_pod(name="p1", cpu="2", memory="1Gi", age_seconds=60),
            make_pending_pod(name="p2", cpu="2", memory="1Gi", age_seconds=60),
        ]

        apply_pending_phantom_load(states, pods, self.cfg)

        phantom_a = [p for p in node_a.pods if p.is_phantom]
        phantom_b = [p for p in node_b.pods if p.is_phantom]
        # One pod on each node (spread)
        self.assertEqual(len(phantom_a), 1)
        self.assertEqual(len(phantom_b), 1)


# ============================================================================
# Integration: phantom load prevents tainting
# ============================================================================


class TestPhantomPreventsTagging(unittest.TestCase):
    def test_phantom_load_increases_utilization(self):
        """Phantom load should raise node utilization, potentially preventing tainting."""
        node = make_node_state(
            name="node-1",
            allocatable_cpu=10.0,
            allocatable_memory=40 * GiB,
            pods=[],
        )
        states = {"node-1": node}

        # Node starts at 0% utilization — would normally be a taint candidate
        self.assertAlmostEqual(node.utilization, 0.0)

        # After phantom load, utilization should be > 0
        pod = make_pending_pod(cpu="3", memory="10Gi", age_seconds=60)
        apply_pending_phantom_load(states, [pod], make_config())

        # Now utilization is 30% CPU or 25% memory = 30%
        self.assertAlmostEqual(node.cpu_utilization, 0.3)
        self.assertAlmostEqual(node.memory_utilization, 10 * GiB / (40 * GiB))
        self.assertGreater(node.utilization, 0.0)


if __name__ == "__main__":
    unittest.main()
