"""Node-compactor end-to-end tests.

Three sequential test groups that validate the compactor's distinct modes:

  Group A (Bare):        Core taint logic, no anti-flap, no reservation
  Group B (Anti-flap):   min_node_age, rate limiting, fleet cooldown
  Group C (Reservation): Capacity reservation with do-not-disrupt

Each group builds on the previous one. Stop on first failure (-x).

Expected runtime: ~20 minutes (dominated by Karpenter provisioning in
Group A; Groups B and C reuse existing nodes via config switch).
"""

from __future__ import annotations

import logging
import time

import pytest
from conftest import (
    GROUP_B_MIN_AGE_CONFIG,
    GROUP_B_RATE_COOLDOWN_CONFIG,
    GROUP_C_CONFIG,
    CompactorLogCollector,
)
from helpers import (
    COMPACTOR_TAINT_KEY,
    all_pods_running,
    assert_instance_type_taints_preserved,
    create_test_pod,
    delete_all_pods,
    delete_pods,
    get_do_not_disrupt_nodes,
    get_pods_by_node,
    get_pool_nodes,
    get_reserved_nodes,
    get_tainted_nodes,
    partition_pool_nodes,
    reconfigure_compactor,
    scale_compactor_deployment,
    search_compactor_logs,
    wait_for,
    wait_for_compactor_fully_terminated,
    wait_for_pods_deleted,
    wait_for_stable,
)
from lightkube import Client
from lightkube.resources.core_v1 import Pod as PodResource

log = logging.getLogger("e2e")

# Pod sizing: 30 CPU / 120Gi -> 3 pods per r5.24xlarge (96 vCPU, 768 GiB)
POD_CPU = "30"
POD_MEMORY = "120Gi"

# Timeouts
PROVISION_TIMEOUT = 600  # 10 min for Karpenter to provision nodes
TAINT_TIMEOUT = 90  # 90s for compactor to taint (interval=5s)
KARPENTER_DELETE_TIMEOUT = 360  # 6 min for WhenEmpty + consolidateAfter
BURST_TIMEOUT = 180  # 3 min for burst absorption
COMPACTOR_CYCLE = 5  # seconds (matches COMPACTOR_INTERVAL override)
STABLE_WINDOW = COMPACTOR_CYCLE * 2  # taint state must be stable for this long


def _reserved_subset_of_untainted(client: Client, pool: str) -> bool:
    """Check reserved ⊆ untainted from a SINGLE API snapshot.

    Calling get_reserved_nodes() and partition_pool_nodes() separately
    creates a TOCTOU race — the compactor can change reservation/taint
    state between the two calls.  This reads nodes once and derives both
    sets from the same snapshot.
    """
    nodes = get_pool_nodes(client, pool)
    reserved: set[str] = set()
    tainted: set[str] = set()
    for node in nodes:
        name = node.metadata.name if node.metadata else ""
        annotations = (node.metadata and node.metadata.annotations) or {}
        if annotations.get("node-compactor.osdc.io/capacity-reserved") == "true":
            reserved.add(name)
        if node.spec and node.spec.taints and any(t.key == COMPACTOR_TAINT_KEY for t in node.spec.taints):
            tainted.add(name)
    if not reserved:
        return False  # no reserved nodes yet — keep waiting
    return not reserved.intersection(tainted)


def _count_running_pods(client: Client, namespace: str) -> int:
    """Count pods in *namespace* that are in Running phase."""
    pods = list(client.list(PodResource, namespace=namespace))
    return sum(1 for p in pods if p.status and p.status.phase == "Running")


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
        create_test_pod(client, name, namespace, nodepool, instance_type, POD_CPU, POD_MEMORY)
        names.append(name)
    return names


def _ensure_pods_and_nodes(
    client: Client,
    namespace: str,
    nodepool: str,
    instance_type: str,
    target_pods: int = 9,
    min_nodes: int = 3,
    prefix: str = "topup",
) -> None:
    """Ensure at least *target_pods* Running and *min_nodes* in pool.

    Creates only the shortfall. Waits for provisioning if needed.
    Stabilises the pool first to avoid racing with Karpenter WhenEmpty
    consolidation (empty nodes left by a prior group may still be
    mid-deletion).
    """
    wait_for_stable(
        "pool node count stabilised (WhenEmpty settling)",
        lambda: len(get_pool_nodes(client, nodepool)),
        stable_s=20,
        timeout_s=300,
        poll_s=5,
    )
    running = _count_running_pods(client, namespace)
    shortfall = max(0, target_pods - running)
    if shortfall > 0:
        log.info("Topping up: creating %d pods (have %d)", shortfall, running)
        _create_pods(client, namespace, nodepool, instance_type, shortfall, prefix)

    current_nodes = len(get_pool_nodes(client, nodepool))
    if current_nodes < min_nodes or shortfall > 0:
        wait_for(
            f">= {min_nodes} nodes in pool",
            lambda: len(get_pool_nodes(client, nodepool)) >= min_nodes,
            timeout_s=PROVISION_TIMEOUT,
            poll_s=5,
        )
        wait_for(
            f"{target_pods} pods running",
            lambda: all_pods_running(client, namespace, target_pods),
            timeout_s=PROVISION_TIMEOUT,
            poll_s=5,
        )


# ---------------------------------------------------------------------------
# Base class for shared fixture injection
# ---------------------------------------------------------------------------


class _CompactorE2EBase:
    """Inject session fixtures onto ``self`` for all test groups."""

    @pytest.fixture(autouse=True)
    def _inject(
        self,
        client: Client,
        test_namespace: str,
        target_nodepool_name: str,
        instance_type: str,
        compactor_logs: CompactorLogCollector,
    ) -> None:
        self.client = client
        self.ns = test_namespace
        self.pool = target_nodepool_name
        self.itype = instance_type
        self.logs = compactor_logs

    def _taint_diagnostics(self) -> str:
        """Dump current pool state for timeout diagnostics."""
        nodes, tainted, untainted = partition_pool_nodes(self.client, self.pool)
        pods_by_node = get_pods_by_node(self.client, self.ns)
        lines = [
            f"Pool {self.pool}: {len(nodes)} nodes, {len(tainted)} tainted, {len(untainted)} untainted",
            f"Tainted: {tainted}",
            f"Untainted: {untainted}",
            f"Pods by node: {pods_by_node}",
        ]
        all_pods = list(self.client.list(PodResource, namespace=self.ns))
        terminating = [p.metadata.name for p in all_pods if p.metadata and p.metadata.deletionTimestamp]
        if terminating:
            lines.append(f"Terminating pods: {terminating}")
        return "\n".join(lines)


# ============================================================================
# Group A: Bare Compactor (no anti-flap, no reservation)
# ============================================================================


class TestGroupA_Bare(_CompactorE2EBase):
    """Core compactor logic with all safety mechanisms disabled."""

    # A1
    def test_scale_up_no_over_taint(self) -> None:
        """9 pods across multiple nodes. At least 1 untainted, no reservations."""
        log.info("A1: Creating 9 test pods...")
        _create_pods(self.client, self.ns, self.pool, self.itype, 9, "a1")

        wait_for(
            ">= 3 nodes in pool",
            lambda: len(get_pool_nodes(self.client, self.pool)) >= 3,
            timeout_s=PROVISION_TIMEOUT,
            poll_s=5,
        )
        wait_for(
            "9 pods running",
            lambda: all_pods_running(self.client, self.ns, 9),
            timeout_s=PROVISION_TIMEOUT,
            poll_s=5,
        )

        wait_for_stable(
            "compactor taint state after scale-up",
            lambda: (
                sorted(get_tainted_nodes(self.client, self.pool)),
                len(get_pool_nodes(self.client, self.pool)),
            ),
            stable_s=STABLE_WINDOW,
            timeout_s=TAINT_TIMEOUT,
        )

        nodes, tainted, untainted = partition_pool_nodes(self.client, self.pool)
        log.info("A1 result: %d nodes, %d tainted, %d untainted", len(nodes), len(tainted), len(untainted))
        assert len(nodes) >= 3, f"Expected >= 3 nodes, got {len(nodes)}"
        assert len(untainted) >= 1, f"Expected >= 1 untainted (min_nodes), got {len(untainted)}"
        assert all_pods_running(self.client, self.ns, 9), "Expected all 9 pods still running"

        # No reservations in Group A
        reserved = get_reserved_nodes(self.client, self.pool)
        assert len(reserved) == 0, f"Expected 0 reserved nodes in Group A, got {len(reserved)}: {reserved}"

    # A2
    def test_empty_nodes_get_tainted(self) -> None:
        """Remove pods from all-but-one node -> drained nodes get tainted."""
        initial_tainted = set(get_tainted_nodes(self.client, self.pool))
        log.info("A2: Initially tainted nodes: %s", initial_tainted)

        pods_by_node = get_pods_by_node(self.client, self.ns)
        nodes_with_pods = sorted(pods_by_node.keys())
        assert len(nodes_with_pods) >= 2, f"Expected pods on >= 2 nodes, got {len(nodes_with_pods)}"

        # Keep 1 node loaded, drain all others
        nodes_to_drain = nodes_with_pods[:-1]
        pods_to_delete = []
        for node in nodes_to_drain:
            pods_to_delete.extend(pods_by_node[node])

        log.info("A2: Deleting %d pods from %d nodes (keeping 1 loaded)", len(pods_to_delete), len(nodes_to_drain))
        delete_pods(self.client, self.ns, pods_to_delete)
        wait_for_pods_deleted(self.client, self.ns, pods_to_delete)

        drained_set = set(nodes_to_drain)
        wait_for(
            f"drained nodes {drained_set} tainted or deleted",
            lambda: all(
                n in set(get_tainted_nodes(self.client, self.pool))
                or n not in {nd.metadata.name for nd in get_pool_nodes(self.client, self.pool)}
                for n in drained_set
            ),
            timeout_s=TAINT_TIMEOUT,
            on_timeout=self._taint_diagnostics,
        )

        nodes, tainted, untainted = partition_pool_nodes(self.client, self.pool)
        log.info("A2 result: %d nodes, %d tainted, %d untainted", len(nodes), len(tainted), len(untainted))
        surviving_drained = drained_set & {n.metadata.name for n in nodes}
        assert surviving_drained.issubset(set(tainted)), (
            f"Expected surviving drained nodes {surviving_drained} in tainted set {tainted}"
        )
        assert len(untainted) >= 1, f"Expected >= 1 untainted, got {len(untainted)}"

        # Regression: compactor taint ops must never wipe instance-type taints
        assert_instance_type_taints_preserved(self.client, self.pool)

    # A3
    def test_karpenter_deletes_empty_tainted_nodes(self) -> None:
        """After consolidateAfter, empty tainted nodes are removed."""
        wait_for(
            "all tainted nodes deleted",
            lambda: len(get_tainted_nodes(self.client, self.pool)) == 0,
            timeout_s=KARPENTER_DELETE_TIMEOUT,
            poll_s=5,
        )

        nodes, tainted, _untainted = partition_pool_nodes(self.client, self.pool)
        log.info("A3 result: %d nodes remain, %d tainted", len(nodes), len(tainted))
        assert len(nodes) >= 1, f"Expected >= 1 surviving node, got {len(nodes)}"
        assert len(tainted) == 0, f"Expected 0 tainted, got {len(tainted)}: {tainted}"

    # A4
    def test_burst_absorption(self) -> None:
        """Tainted nodes get untainted when new pods are pending."""
        _ensure_pods_and_nodes(self.client, self.ns, self.pool, self.itype, prefix="a4a")

        wait_for_stable(
            "compactor taint state before burst drain",
            lambda: (
                sorted(get_tainted_nodes(self.client, self.pool)),
                len(get_pool_nodes(self.client, self.pool)),
            ),
            stable_s=STABLE_WINDOW,
            timeout_s=TAINT_TIMEOUT,
        )
        assert all_pods_running(self.client, self.ns, 9), "Expected 9 pods running after stabilization"

        # Drain all-but-one node -> trigger tainting
        pods_by_node = get_pods_by_node(self.client, self.ns)
        nodes_with_pods = sorted(pods_by_node.keys())
        nodes_to_drain = nodes_with_pods[:-1]
        surviving_pods = len(pods_by_node[nodes_with_pods[-1]])

        all_deleted: list[str] = []
        for node in nodes_to_drain:
            delete_pods(self.client, self.ns, pods_by_node[node])
            all_deleted.extend(pods_by_node[node])
        wait_for_pods_deleted(self.client, self.ns, all_deleted)

        n_drained = len(nodes_to_drain)
        log.info("A4: Drained %d nodes, %d pods survive", n_drained, surviving_pods)

        wait_for(
            f"{n_drained} nodes tainted",
            lambda: len(get_tainted_nodes(self.client, self.pool)) >= n_drained,
            timeout_s=TAINT_TIMEOUT,
            on_timeout=self._taint_diagnostics,
        )

        # Create burst pods to fill the tainted nodes
        burst_count = 9 - surviving_pods
        log.info("A4: Creating %d burst pods...", burst_count)
        _create_pods(self.client, self.ns, self.pool, self.itype, burst_count, "a4b")

        total_expected = surviving_pods + burst_count
        wait_for(
            f"all {total_expected} pods running after burst absorption",
            lambda: all_pods_running(self.client, self.ns, total_expected),
            timeout_s=BURST_TIMEOUT,
            poll_s=5,
        )
        assert all_pods_running(self.client, self.ns, total_expected), (
            f"Expected {total_expected} pods running after burst absorption"
        )

        # Regression: burst untaint must not wipe instance-type taints
        assert_instance_type_taints_preserved(self.client, self.pool)

    # A5
    def test_min_nodes_enforcement(self) -> None:
        """Delete all pods -> at least 1 node stays untainted. No reservations."""
        log.info("A5: Deleting all test pods...")
        delete_all_pods(self.client, self.ns)

        wait_for_stable(
            "compactor taint state after pod deletion",
            lambda: (
                sorted(get_tainted_nodes(self.client, self.pool)),
                len(get_pool_nodes(self.client, self.pool)),
            ),
            stable_s=STABLE_WINDOW,
            timeout_s=TAINT_TIMEOUT,
        )

        # Poll for min_nodes enforcement — the compactor may need an extra
        # cycle to stabilize after Karpenter scales down empty tainted nodes.
        def _check_min_nodes() -> bool:
            ns, _t, ut = partition_pool_nodes(self.client, self.pool)
            return len(ns) == 0 or len(ut) >= 1

        wait_for(
            "at least 1 untainted node (min_nodes enforcement)",
            _check_min_nodes,
            timeout_s=TAINT_TIMEOUT,
            poll_s=5,
        )

        nodes, _tainted, untainted = partition_pool_nodes(self.client, self.pool)
        if len(nodes) == 0:
            pytest.skip("Pool has 0 nodes (Karpenter scaled down)")

        assert len(untainted) >= 1, f"Expected >= 1 untainted (min_nodes), got {len(untainted)}"

        # No reservations in Group A
        reserved = get_reserved_nodes(self.client, self.pool)
        assert len(reserved) == 0, f"Expected 0 reserved nodes in Group A, got {len(reserved)}: {reserved}"

    # A6
    def test_sigterm_removes_taints(self) -> None:
        """Scale compactor to 0 -> SIGTERM cleanup -> verify taints removed."""
        nodes, tainted, _untainted = partition_pool_nodes(self.client, self.pool)
        if len(nodes) == 0:
            pytest.skip("Pool has 0 nodes; cannot verify taint cleanup")

        if not tainted:
            log.info("A6: No tainted nodes, waiting for compactor cycle...")
            try:
                wait_for(
                    "at least 1 tainted node for cleanup test",
                    lambda: len(get_tainted_nodes(self.client, self.pool)) > 0,
                    timeout_s=TAINT_TIMEOUT,
                    poll_s=5,
                )
            except TimeoutError:
                pytest.skip("No tainted nodes available to verify cleanup")

        nodes_before, tainted_before, _ = partition_pool_nodes(self.client, self.pool)
        node_names_before = {n.metadata.name for n in nodes_before}
        log.info("A6: %d nodes, %d tainted before shutdown", len(nodes_before), len(tainted_before))

        log.info("A6: Scaling compactor to 0 (SIGTERM)...")
        scale_compactor_deployment(self.client, 0)
        try:
            wait_for(
                "all compactor taints cleared after graceful shutdown",
                lambda: len(get_tainted_nodes(self.client, self.pool)) == 0,
                timeout_s=TAINT_TIMEOUT,
                poll_s=5,
            )

            nodes_after, tainted_after, _untainted_after = partition_pool_nodes(self.client, self.pool)
            surviving_tainted = node_names_before & set(tainted_before)
            surviving_nodes = {n.metadata.name for n in nodes_after}
            cleaned = surviving_tainted & surviving_nodes
            log.info("A6 result: %d nodes remain, %d cleaned", len(nodes_after), len(cleaned))
            if cleaned:
                assert not cleaned.intersection(set(tainted_after)), (
                    f"Nodes {cleaned & set(tainted_after)} still tainted after shutdown"
                )
            assert len(tainted_after) == 0, f"Expected 0 tainted after cleanup, got {len(tainted_after)}"

            # Regression: SIGTERM cleanup must not wipe instance-type taints
            assert_instance_type_taints_preserved(self.client, self.pool)
        finally:
            log.info("A6: Scaling compactor back to 1...")
            scale_compactor_deployment(self.client, 1)


# ============================================================================
# Group B: Anti-Flap Mechanisms
# ============================================================================


class TestGroupB_AntiFlap(_CompactorE2EBase):
    """Test min_node_age, rate limiting, and fleet cooldown."""

    # B1
    def test_min_node_age_blocks_tainting(self) -> None:
        """min_node_age=3600 prevents tainting nodes younger than 1 hour."""
        # Switch to min_node_age=3600 config. Nodes from Group A are ~15-20 min
        # old — well under the 3600s threshold.
        log.info("B1: Switching to GROUP_B_MIN_AGE_CONFIG (min_node_age=3600)...")
        reconfigure_compactor(self.client, GROUP_B_MIN_AGE_CONFIG, self.logs)

        # Ensure we have pods and nodes
        _ensure_pods_and_nodes(self.client, self.ns, self.pool, self.itype, prefix="b1a")

        # Drain 2 nodes to create surplus
        pods_by_node = get_pods_by_node(self.client, self.ns)
        nodes_with_pods = sorted(pods_by_node.keys())
        assert len(nodes_with_pods) >= 2, f"Expected pods on >= 2 nodes, got {len(nodes_with_pods)}"

        nodes_to_drain = nodes_with_pods[:-1]
        pods_to_delete = []
        for node in nodes_to_drain:
            pods_to_delete.extend(pods_by_node[node])

        log.info("B1: Draining %d nodes (keeping 1 loaded)", len(nodes_to_drain))
        delete_pods(self.client, self.ns, pods_to_delete)
        wait_for_pods_deleted(self.client, self.ns, pods_to_delete)

        # Wait 3 compactor cycles — nodes should NOT be tainted
        log.info("B1: Waiting for compactor to stabilize (expecting no taints)...")
        drained_set = set(nodes_to_drain)
        wait_for_stable(
            "drained nodes remain untainted (min_node_age protection)",
            lambda: (
                sorted(get_tainted_nodes(self.client, self.pool)),
                len(get_pool_nodes(self.client, self.pool)),
            ),
            stable_s=COMPACTOR_CYCLE * 3,
            timeout_s=TAINT_TIMEOUT,
        )

        # Assert: drained nodes that still exist are NOT tainted
        nodes, tainted, _untainted = partition_pool_nodes(self.client, self.pool)
        surviving_drained = drained_set & {n.metadata.name for n in nodes}
        newly_tainted = surviving_drained & set(tainted)
        assert len(newly_tainted) == 0, f"Expected 0 drained nodes tainted with min_node_age=3600, got {newly_tainted}"

        # Now switch to min_node_age=0 — same nodes should get tainted immediately
        log.info("B1: Switching to min_node_age=0...")
        reconfigure_compactor(self.client, {**GROUP_B_MIN_AGE_CONFIG, "COMPACTOR_MIN_NODE_AGE": "0"}, self.logs)

        wait_for(
            f"drained nodes {surviving_drained} tainted or deleted (min_node_age=0)",
            lambda: all(
                n in set(get_tainted_nodes(self.client, self.pool))
                or n not in {nd.metadata.name for nd in get_pool_nodes(self.client, self.pool)}
                for n in surviving_drained
            ),
            timeout_s=TAINT_TIMEOUT,
            on_timeout=self._taint_diagnostics,
        )
        log.info("B1: Drained nodes tainted after removing age protection.")

    # B2
    def test_rate_limiting(self) -> None:
        """taint_rate=0.25 caps new taints per iteration."""
        log.info("B2: Switching to GROUP_B_RATE_COOLDOWN_CONFIG...")
        reconfigure_compactor(self.client, GROUP_B_RATE_COOLDOWN_CONFIG, self.logs)

        # Use 15 pods / 5 nodes so the surplus is large enough to guarantee
        # rate-limiting even when the compactor races and taints a node during
        # sequential pod deletion.
        #   pool_size=5, min_nodes=1 -> surplus=4
        #   max_new_taints = max(1, ceil(4 * 0.25)) = 1
        #   Even if 1 node gets race-tainted, 3 new nodes still need tainting
        #   but the cap is 1, so at least 2 MUST be rate-limited.
        _ensure_pods_and_nodes(
            self.client,
            self.ns,
            self.pool,
            self.itype,
            prefix="b2",
            target_pods=15,
            min_nodes=5,
        )

        wait_for_stable(
            "compactor stabilized before rate limit test",
            lambda: (
                sorted(get_tainted_nodes(self.client, self.pool)),
                len(get_pool_nodes(self.client, self.pool)),
            ),
            stable_s=STABLE_WINDOW,
            timeout_s=TAINT_TIMEOUT,
        )

        # Record log position before the test action
        log_pos = len(self.logs.lines)

        # Delete ALL pods simultaneously — 4 surplus nodes (5 - min_nodes=1)
        # taint_rate=0.25 -> max_new_taints = max(1, ceil(4*0.25)) = 1
        # So at least 3 nodes MUST be rate-limited on cycle 1
        log.info("B2: Deleting all pods to create 4 surplus nodes...")
        delete_all_pods(self.client, self.ns)

        # Wait for all surplus nodes to eventually be tainted (may take 2+ cycles)
        wait_for_stable(
            "all surplus nodes tainted (rate-limited across cycles)",
            lambda: (
                sorted(get_tainted_nodes(self.client, self.pool)),
                len(get_pool_nodes(self.client, self.pool)),
            ),
            stable_s=STABLE_WINDOW,
            timeout_s=TAINT_TIMEOUT,
        )

        # Assert: rate limiting fired (deterministic: 4 surplus, cap is 1).
        # Poll instead of asserting immediately — log lines may be delayed
        # by pipe buffering between kubectl and the collector thread.
        wait_for(
            "Rate-limited log entry captured",
            lambda: len(search_compactor_logs(self.logs, r"Rate-limited", log_pos)) > 0,
            timeout_s=60,
            poll_s=2,
        )
        rate_limited_lines = search_compactor_logs(self.logs, r"Rate-limited", log_pos)
        log.info("B2: Rate limiting confirmed: %d log entries", len(rate_limited_lines))

    # B3
    def test_fleet_cooldown_blocks_after_burst(self) -> None:
        """Fleet cooldown blocks tainting after a burst untaint."""
        # From B2 end state: surplus tainted empty nodes.
        # Create 9 pods -> go Pending -> burst untaint triggers
        log.info("B3: Creating 9 pods for burst absorption...")
        _create_pods(self.client, self.ns, self.pool, self.itype, 9, "b3a")

        wait_for(
            "all 9 pods running after burst",
            lambda: all_pods_running(self.client, self.ns, 9),
            timeout_s=BURST_TIMEOUT,
            poll_s=5,
        )

        # Record log position after burst absorption
        log_pos = len(self.logs.lines)

        # Now drain 2 nodes to create surplus again
        pods_by_node = get_pods_by_node(self.client, self.ns)
        nodes_with_pods = sorted(pods_by_node.keys())
        nodes_to_drain = nodes_with_pods[:-1]
        pods_to_delete = []
        for node in nodes_to_drain:
            pods_to_delete.extend(pods_by_node[node])

        log.info("B3: Draining %d nodes post-burst...", len(nodes_to_drain))
        delete_pods(self.client, self.ns, pods_to_delete)
        wait_for_pods_deleted(self.client, self.ns, pods_to_delete)

        # Wait for those nodes to eventually be tainted.
        # fleet_cooldown=90s blocks tainting for ~90s after burst untaint,
        # then compactor proceeds. Use a longer timeout to account for this.
        drained_set = set(nodes_to_drain)
        wait_for(
            f"drained nodes {drained_set} tainted or deleted after fleet cooldown",
            lambda: all(
                n in set(get_tainted_nodes(self.client, self.pool))
                or n not in {nd.metadata.name for nd in get_pool_nodes(self.client, self.pool)}
                for n in drained_set
            ),
            timeout_s=180,
            on_timeout=self._taint_diagnostics,
        )

        # Assert: fleet cooldown log message fired.
        # Poll instead of asserting immediately — log lines may be delayed
        # by pipe buffering between kubectl and the collector thread.
        wait_for(
            "Fleet cooldown blocked log entry captured",
            lambda: len(search_compactor_logs(self.logs, r"Fleet cooldown blocked", log_pos)) > 0,
            timeout_s=30,
            poll_s=2,
        )
        cooldown_lines = search_compactor_logs(self.logs, r"Fleet cooldown blocked", log_pos)
        log.info("B3: Fleet cooldown confirmed: %d log entries", len(cooldown_lines))


# ============================================================================
# Group C: Reservation Behavior
# ============================================================================


class TestGroupC_Reservation(_CompactorE2EBase):
    """Test capacity reservation with do-not-disrupt annotations."""

    # C1
    def test_reserved_node_excluded_from_taint(self) -> None:
        """With reservation=1, one node gets reserved and is excluded from taint."""
        log.info("C1: Switching to GROUP_C_CONFIG (reservation=1)...")
        reconfigure_compactor(self.client, GROUP_C_CONFIG, self.logs)

        # Ensure 9 pods / 3+ nodes
        _ensure_pods_and_nodes(self.client, self.ns, self.pool, self.itype, prefix="c1a")

        # Wait for compactor to stabilize with new config
        wait_for_stable(
            "compactor stabilized with reservation config",
            lambda: (
                sorted(get_tainted_nodes(self.client, self.pool)),
                len(get_pool_nodes(self.client, self.pool)),
                sorted(get_reserved_nodes(self.client, self.pool)),
            ),
            stable_s=STABLE_WINDOW,
            timeout_s=TAINT_TIMEOUT,
        )

        # Assert: exactly 1 reserved node with both annotations.
        # The compactor may need an extra cycle after taint state stabilizes
        # to apply reservation annotations — poll instead of asserting immediately.
        wait_for(
            "at least 1 reserved node",
            lambda: len(get_reserved_nodes(self.client, self.pool)) >= 1,
            timeout_s=TAINT_TIMEOUT,
            poll_s=5,
        )
        reserved = get_reserved_nodes(self.client, self.pool)
        do_not_disrupt = get_do_not_disrupt_nodes(self.client, self.pool)
        assert len(reserved) == 1, f"Expected exactly 1 reserved node, got {len(reserved)}: {reserved}"
        assert reserved.issubset(do_not_disrupt), (
            f"Reserved node(s) {reserved} should have do-not-disrupt, got dnd={do_not_disrupt}"
        )

        # Drain all-but-one node (including reserved node if it has pods)
        pods_by_node = get_pods_by_node(self.client, self.ns)
        nodes_with_pods = sorted(pods_by_node.keys())
        nodes_to_drain = nodes_with_pods[:-1]
        pods_to_delete = []
        for node in nodes_to_drain:
            pods_to_delete.extend(pods_by_node[node])

        log.info("C1: Draining %d nodes (keeping 1 loaded)", len(nodes_to_drain))
        delete_pods(self.client, self.ns, pods_to_delete)
        wait_for_pods_deleted(self.client, self.ns, pods_to_delete)

        # Wait for compactor to stabilize
        wait_for_stable(
            "compactor taint state after drain with reservation",
            lambda: (
                sorted(get_tainted_nodes(self.client, self.pool)),
                len(get_pool_nodes(self.client, self.pool)),
                sorted(get_reserved_nodes(self.client, self.pool)),
            ),
            stable_s=STABLE_WINDOW,
            timeout_s=TAINT_TIMEOUT,
        )

        # Assert: reserved node is NOT tainted
        reserved = get_reserved_nodes(self.client, self.pool)
        tainted = set(get_tainted_nodes(self.client, self.pool))
        assert len(reserved) >= 1, "Expected at least 1 reserved node after drain"
        assert not reserved.intersection(tainted), (
            f"Reserved nodes {reserved} should NOT be tainted, but found in tainted set {tainted}"
        )

    # C2
    def test_min_nodes_with_reservation(self) -> None:
        """Delete all pods -> min_nodes untainted + reservation active."""
        log.info("C2: Deleting all test pods...")
        delete_all_pods(self.client, self.ns)

        wait_for_stable(
            "compactor taint state after pod deletion with reservation",
            lambda: (
                sorted(get_tainted_nodes(self.client, self.pool)),
                len(get_pool_nodes(self.client, self.pool)),
                sorted(get_reserved_nodes(self.client, self.pool)),
            ),
            stable_s=STABLE_WINDOW,
            timeout_s=TAINT_TIMEOUT,
        )

        nodes, _tainted, untainted = partition_pool_nodes(self.client, self.pool)
        if len(nodes) == 0:
            pytest.skip("Pool has 0 nodes (Karpenter scaled down)")

        assert len(untainted) >= 1, f"Expected >= 1 untainted (min_nodes), got {len(untainted)}"

        # Reservation must be active and overlap with untainted nodes.
        # The compactor may need one more cycle to untaint a newly-reserved
        # node (reservation annotation is written after taints in the same
        # cycle, so the mandatory untaint fires on the *next* cycle).
        do_not_disrupt = get_do_not_disrupt_nodes(self.client, self.pool)
        assert len(do_not_disrupt) >= 1, f"Expected >= 1 node with do-not-disrupt annotation, got {len(do_not_disrupt)}"
        # Use atomic snapshot to avoid TOCTOU race between separate
        # get_reserved_nodes() and partition_pool_nodes() API calls.
        wait_for(
            "reserved nodes are untainted",
            lambda: _reserved_subset_of_untainted(self.client, self.pool),
            timeout_s=TAINT_TIMEOUT,
            poll_s=5,
        )

    # C3
    def test_reservation_cleanup_on_shutdown(self) -> None:
        """SIGTERM removes reservation annotations and taints."""
        # Verify reservation is active
        reserved_before = get_reserved_nodes(self.client, self.pool)
        if not reserved_before:
            # Wait briefly for compactor to set reservation
            try:
                wait_for(
                    "at least 1 reserved node",
                    lambda: len(get_reserved_nodes(self.client, self.pool)) > 0,
                    timeout_s=TAINT_TIMEOUT,
                    poll_s=5,
                )
                reserved_before = get_reserved_nodes(self.client, self.pool)
            except TimeoutError:
                pytest.skip("No reserved nodes available for cleanup test")

        log.info("C3: %d reserved nodes before shutdown: %s", len(reserved_before), reserved_before)

        log.info("C3: Scaling compactor to 0 (SIGTERM)...")
        scale_compactor_deployment(self.client, 0)
        # scale_compactor_deployment returns when pods have deletionTimestamp,
        # but shutdown cleanup (taint + reservation removal) may still be
        # running. Wait for pods to be fully gone before checking results.
        wait_for_compactor_fully_terminated(self.client)
        try:
            # Verify taints removed
            wait_for(
                "all compactor taints cleared after shutdown",
                lambda: len(get_tainted_nodes(self.client, self.pool)) == 0,
                timeout_s=TAINT_TIMEOUT,
                poll_s=5,
                on_timeout=lambda: f"Remaining tainted: {get_tainted_nodes(self.client, self.pool)}",
            )

            # Verify reservation annotations removed — cleanup runs sequentially
            # after taint removal in the shutdown handler, so poll for completion.
            wait_for(
                "reservation annotations cleared after shutdown",
                lambda: len(get_reserved_nodes(self.client, self.pool)) == 0,
                timeout_s=TAINT_TIMEOUT,
                poll_s=5,
                on_timeout=lambda: f"Remaining reserved: {get_reserved_nodes(self.client, self.pool)}",
            )

            # do-not-disrupt annotations (that the compactor set) should also be gone
            dnd_after = get_do_not_disrupt_nodes(self.client, self.pool)
            # Note: other controllers may set do-not-disrupt. We only check that
            # nodes that were reserved by the compactor no longer have it.
            leftover = reserved_before & dnd_after
            assert len(leftover) == 0, (
                f"Expected compactor's do-not-disrupt annotations removed, but found on {leftover}"
            )
        finally:
            log.info("C3: Scaling compactor back to 1...")
            scale_compactor_deployment(self.client, 1)
