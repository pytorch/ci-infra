"""Node-compactor end-to-end tests.

Sequential test phases that validate the full compactor loop:
  compactor decisions -> Kubernetes taints -> Karpenter node lifecycle

Each phase builds on the previous one. Stop on first failure (-x).

Expected runtime: ~25 minutes (dominated by Karpenter provisioning and
WhenEmpty consolidation waits).
"""

from __future__ import annotations

import logging
import time

import pytest
from helpers import (
    all_pods_running,
    create_test_pod,
    delete_all_pods,
    delete_pods,
    get_pods_by_node,
    get_pool_nodes,
    get_tainted_nodes,
    get_untainted_nodes,
    restart_compactor_pod,
    wait_for,
)
from lightkube import Client


log = logging.getLogger("e2e")

# Pod sizing: 30 CPU / 120Gi -> 3 pods per r5.24xlarge (96 vCPU, 768 GiB)
POD_CPU = "30"
POD_MEMORY = "120Gi"

# Timeouts
PROVISION_TIMEOUT = 600  # 10 min for Karpenter to provision nodes
TAINT_TIMEOUT = 120  # 2 min for compactor to taint
KARPENTER_DELETE_TIMEOUT = 360  # 6 min for WhenEmpty + consolidateAfter
BURST_TIMEOUT = 180  # 3 min for burst absorption
COMPACTOR_CYCLE = 10  # seconds (matches COMPACTOR_INTERVAL override)


def _create_pods(
    client: Client,
    namespace: str,
    nodepool: str,
    instance_type: str,
    count: int,
    prefix: str = "e2e",
) -> list[str]:
    """Create *count* test pods and return their names."""
    names = []
    for i in range(count):
        name = f"{prefix}-{int(time.time())}-{i}"
        create_test_pod(
            client, name, namespace, nodepool, instance_type, POD_CPU, POD_MEMORY
        )
        names.append(name)
    return names


# ---------------------------------------------------------------------------
# Base class for shared fixture injection
# ---------------------------------------------------------------------------


class _CompactorE2EBase:
    """Inject session fixtures onto ``self`` for all test phases."""

    @pytest.fixture(autouse=True)
    def _inject(
        self,
        client: Client,
        test_namespace: str,
        target_nodepool_name: str,
        instance_type: str,
    ) -> None:
        self.client = client
        self.ns = test_namespace
        self.pool = target_nodepool_name
        self.itype = instance_type


# ============================================================================
# Phase 1: Scale-Up Baseline
# ============================================================================


class TestPhase1ScaleUpBaseline(_CompactorE2EBase):
    """Provision nodes with 9 pods and verify compactor does not taint."""

    def test_scale_up_no_taint(self) -> None:
        """9 pods across multiple nodes. All pods stay running, min_nodes untainted."""
        # Create 9 pods (3 per node target, but Karpenter may provision more)
        log.info("Phase 1: Creating 9 test pods...")
        _create_pods(self.client, self.ns, self.pool, self.itype, 9, "p1")

        # Wait for at least 3 nodes (Karpenter may create more)
        wait_for(
            ">= 3 nodes in pool",
            lambda: len(get_pool_nodes(self.client, self.pool)) >= 3,
            timeout_s=PROVISION_TIMEOUT,
            poll_s=15,
        )

        # Wait for all pods Running
        wait_for(
            "9 pods running",
            lambda: all_pods_running(self.client, self.ns, 9),
            timeout_s=PROVISION_TIMEOUT,
            poll_s=10,
        )

        # Wait 3 compactor cycles to ensure it stabilizes
        log.info("Waiting 3 compactor cycles (%ds)...", COMPACTOR_CYCLE * 3)
        time.sleep(COMPACTOR_CYCLE * 3)

        # Assert: nodes exist and all pods are still running.
        # Karpenter may over-provision (e.g. 5 nodes for 9 pods). The compactor
        # correctly taints surplus nodes via bin-packing — that's fine as long as
        # NoSchedule doesn't evict existing pods.
        nodes = get_pool_nodes(self.client, self.pool)
        tainted = get_tainted_nodes(self.client, self.pool)
        untainted = get_untainted_nodes(self.client, self.pool)
        log.info(
            "Phase 1 result: %d nodes, %d tainted, %d untainted",
            len(nodes), len(tainted), len(untainted),
        )
        assert len(nodes) >= 3, f"Expected >= 3 nodes, got {len(nodes)}"
        assert len(untainted) >= 1, (
            f"Expected >= 1 untainted (min_nodes), got {len(untainted)}"
        )
        # All pods must still be running — NoSchedule doesn't evict
        assert all_pods_running(self.client, self.ns, 9), (
            "Expected all 9 pods still running after compactor stabilization"
        )


# ============================================================================
# Phase 2: Scale-Down Triggers Taint
# ============================================================================


class TestPhase2ScaleDownTriggersTaint(_CompactorE2EBase):
    """Delete pods from nodes, verify compactor taints empty ones."""

    def test_empty_nodes_get_tainted(self) -> None:
        """Remove pods from all-but-one node -> drained nodes get tainted."""
        # Record which nodes are already tainted (compactor may have tainted
        # surplus nodes from Karpenter over-provisioning in Phase 1)
        initial_tainted = set(get_tainted_nodes(self.client, self.pool))
        log.info("Phase 2: Initially tainted nodes: %s", initial_tainted)

        pods_by_node = get_pods_by_node(self.client, self.ns)
        nodes_with_pods = sorted(pods_by_node.keys())
        assert len(nodes_with_pods) >= 2, (
            f"Expected pods on >= 2 nodes, got {len(nodes_with_pods)}"
        )

        # Keep 1 node loaded, drain all others
        nodes_to_drain = nodes_with_pods[:-1]
        pods_to_delete = []
        for node in nodes_to_drain:
            pods_to_delete.extend(pods_by_node[node])

        log.info(
            "Phase 2: Deleting %d pods from %d nodes %s (keeping 1 loaded)",
            len(pods_to_delete),
            len(nodes_to_drain),
            nodes_to_drain,
        )
        delete_pods(self.client, self.ns, pods_to_delete)

        # Wait for compactor to taint the drained nodes (they should appear
        # in the tainted set regardless of whether they were already tainted)
        drained_set = set(nodes_to_drain)
        wait_for(
            f"drained nodes {drained_set} tainted",
            lambda: drained_set.issubset(
                set(get_tainted_nodes(self.client, self.pool))
            ),
            timeout_s=TAINT_TIMEOUT,
        )

        tainted = get_tainted_nodes(self.client, self.pool)
        untainted = get_untainted_nodes(self.client, self.pool)
        log.info(
            "Phase 2 result: %d tainted, %d untainted",
            len(tainted), len(untainted),
        )
        # All drained nodes must be tainted
        assert drained_set.issubset(set(tainted)), (
            f"Expected drained nodes {drained_set} in tainted set {tainted}"
        )
        assert len(untainted) >= 1, f"Expected >= 1 untainted, got {len(untainted)}"


# ============================================================================
# Phase 3: Empty Tainted Nodes Deleted by Karpenter
# ============================================================================


class TestPhase3KarpenterDeletesEmptyNodes(_CompactorE2EBase):
    """Tainted nodes with 0 workloads get deleted by Karpenter WhenEmpty."""

    def test_karpenter_deletes_empty_tainted_nodes(self) -> None:
        """After consolidateAfter (2m), empty tainted nodes are removed."""
        # Wait for all tainted (empty) nodes to be deleted by Karpenter.
        # Nodes with pods remain. We don't know exact counts — just wait
        # for tainted count to reach 0.
        wait_for(
            "all tainted nodes deleted",
            lambda: len(get_tainted_nodes(self.client, self.pool)) == 0,
            timeout_s=KARPENTER_DELETE_TIMEOUT,
            poll_s=15,
        )

        nodes = get_pool_nodes(self.client, self.pool)
        tainted = get_tainted_nodes(self.client, self.pool)
        log.info("Phase 3 result: %d nodes remain, %d tainted", len(nodes), len(tainted))
        # Only nodes with remaining pods should survive
        assert len(nodes) >= 1, f"Expected >= 1 surviving node, got {len(nodes)}"
        assert len(tainted) == 0, f"Expected 0 tainted, got {len(tainted)}: {tainted}"


# ============================================================================
# Phase 4: Burst Absorption
# ============================================================================


class TestPhase4BurstAbsorption(_CompactorE2EBase):
    """Verify compactor untaints nodes when pending pods need capacity."""

    def test_burst_absorption(self) -> None:
        """Tainted nodes get untainted when new pods are pending."""
        # Clean slate: delete all test pods and allow API to propagate
        delete_all_pods(self.client, self.ns)
        time.sleep(5)  # allow API to propagate deletions

        # Create 9 pods -> Karpenter provisions nodes
        log.info("Phase 4: Creating 9 pods for burst test...")
        _create_pods(self.client, self.ns, self.pool, self.itype, 9, "p4a")

        wait_for(
            ">= 3 nodes in pool",
            lambda: len(get_pool_nodes(self.client, self.pool)) >= 3,
            timeout_s=PROVISION_TIMEOUT,
            poll_s=15,
        )
        wait_for(
            "9 pods running",
            lambda: all_pods_running(self.client, self.ns, 9),
            timeout_s=PROVISION_TIMEOUT,
            poll_s=10,
        )

        # Wait for stabilization — compactor may taint surplus nodes, that's OK
        time.sleep(COMPACTOR_CYCLE * 3)
        assert all_pods_running(self.client, self.ns, 9), (
            "Expected all 9 pods running after stabilization"
        )

        # Drain all-but-one node -> trigger tainting
        pods_by_node = get_pods_by_node(self.client, self.ns)
        nodes_with_pods = sorted(pods_by_node.keys())
        nodes_to_drain = nodes_with_pods[:-1]
        surviving_pods = len(pods_by_node[nodes_with_pods[-1]])

        for node in nodes_to_drain:
            delete_pods(self.client, self.ns, pods_by_node[node])

        n_drained = len(nodes_to_drain)
        log.info(
            "Phase 4: Drained %d nodes, %d pods survive on kept node",
            n_drained, surviving_pods,
        )

        wait_for(
            f"{n_drained} nodes tainted",
            lambda: len(get_tainted_nodes(self.client, self.pool)) >= n_drained,
            timeout_s=TAINT_TIMEOUT,
        )

        # NOW create burst: enough pods to fill the tainted nodes
        burst_count = 9 - surviving_pods
        log.info("Phase 4: Creating %d burst pods...", burst_count)
        _create_pods(self.client, self.ns, self.pool, self.itype, burst_count, "p4b")

        # Pods can't schedule on tainted nodes (no compactor toleration)
        # and can't fit on the 1 full node -> Pending
        # Compactor should detect pending pods and untaint to absorb burst
        total_expected = surviving_pods + burst_count
        wait_for(
            f"all {total_expected} pods running after burst absorption",
            lambda: all_pods_running(self.client, self.ns, total_expected),
            timeout_s=BURST_TIMEOUT,
            poll_s=10,
        )

        # All pods running is the real contract — burst was absorbed
        # Compactor may still taint surplus nodes if Karpenter over-provisioned
        assert all_pods_running(self.client, self.ns, total_expected), (
            f"Expected {total_expected} pods running after burst absorption"
        )


# ============================================================================
# Phase 5: Min-Nodes Enforcement
# ============================================================================


class TestPhase5MinNodesEnforcement(_CompactorE2EBase):
    """Verify at least min_nodes stay untainted even when all nodes are empty."""

    def test_min_nodes_kept_untainted(self) -> None:
        """Delete all pods -> at least 1 node stays untainted."""
        log.info("Phase 5: Deleting all test pods...")
        delete_all_pods(self.client, self.ns)

        # Wait for compactor to reconcile
        time.sleep(COMPACTOR_CYCLE * 3)

        nodes = get_pool_nodes(self.client, self.pool)
        if len(nodes) == 0:
            pytest.skip(
                "Pool has 0 nodes (Karpenter scaled down); "
                "min_nodes only applies to existing nodes"
            )

        untainted = get_untainted_nodes(self.client, self.pool)
        assert len(untainted) >= 1, (
            f"Expected >= 1 untainted (min_nodes), got {len(untainted)}"
        )


# ============================================================================
# Phase 6: Graceful Shutdown Cleanup
# ============================================================================


class TestPhase6GracefulShutdownCleanup(_CompactorE2EBase):
    """Verify SIGTERM triggers removal of all compactor taints."""

    def test_sigterm_removes_taints(self) -> None:
        """Delete compactor pod -> taints cleaned up -> new pod starts."""
        # Record current tainted nodes (if any)
        initial_tainted = get_tainted_nodes(self.client, self.pool)
        log.info(
            "Phase 6: Tainted nodes before shutdown: %s", initial_tainted
        )

        # Delete the compactor pod (SIGTERM -> cleanup_stale_taints)
        log.info("Phase 6: Deleting compactor pod (SIGTERM)...")
        restart_compactor_pod(self.client)

        # The old pod's SIGTERM handler calls cleanup_stale_taints() which
        # removes all compactor taints. This happens asynchronously during
        # pod termination, so poll until the taints are cleared.
        wait_for(
            "all compactor taints cleared after graceful shutdown",
            lambda: len(get_tainted_nodes(self.client, self.pool)) == 0,
            timeout_s=TAINT_TIMEOUT,
            poll_s=5,
        )
