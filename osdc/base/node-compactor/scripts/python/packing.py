"""Bin-packing and taint computation for the Node Compactor."""

import logging
from collections import defaultdict

from models import Config, NodeState, PodInfo

log = logging.getLogger("compactor")


def bin_pack_min_nodes(pods: list[PodInfo], nodes: list[NodeState]) -> int:
    """First-fit-decreasing bin-pack: minimum nodes needed for all pods.

    Sorts pods largest-first (by CPU), then greedily assigns to nodes
    sorted by capacity (largest-first). Returns how many nodes are needed.

    DaemonSet overhead is subtracted from each node's capacity since
    DaemonSet pods run on every node regardless of scheduling.
    """
    if not pods or not nodes:
        return 0

    sorted_pods = sorted(pods, key=lambda p: p.cpu_request, reverse=True)

    bins: list[dict] = []
    for node in sorted(nodes, key=lambda n: n.allocatable_cpu, reverse=True):
        bins.append(
            {
                "cpu_remaining": node.allocatable_cpu - node.daemonset_cpu,
                "mem_remaining": node.allocatable_memory - node.daemonset_memory,
            }
        )

    nodes_used = 0
    for pod in sorted_pods:
        placed = False
        for b in bins[:nodes_used]:
            if b["cpu_remaining"] >= pod.cpu_request and b["mem_remaining"] >= pod.memory_request:
                b["cpu_remaining"] -= pod.cpu_request
                b["mem_remaining"] -= pod.memory_request
                placed = True
                break
        if not placed:
            if nodes_used < len(bins):
                b = bins[nodes_used]
                b["cpu_remaining"] -= pod.cpu_request
                b["mem_remaining"] -= pod.memory_request
                nodes_used += 1
            else:
                return len(bins)

    return max(nodes_used, 1)


def _pods_fit_on_nodes(pods: list[PodInfo], nodes: list[NodeState]) -> bool:
    """Check if all pods can fit on the given nodes (considering current load).

    Uses total resource usage (workload + DaemonSet) to compute remaining
    capacity, since DaemonSet pods consume real resources on the node.
    """
    if not pods:
        return True
    if not nodes:
        return False

    # Build bins from available capacity on each node, accounting for
    # ALL pods (workload + DaemonSet) currently running
    bins = []
    for node in nodes:
        bins.append(
            {
                "cpu_remaining": node.allocatable_cpu - node.total_cpu_used,
                "mem_remaining": node.allocatable_memory - node.total_memory_used,
            }
        )

    sorted_pods = sorted(pods, key=lambda p: p.cpu_request, reverse=True)
    for pod in sorted_pods:
        placed = False
        for b in bins:
            if b["cpu_remaining"] >= pod.cpu_request and b["mem_remaining"] >= pod.memory_request:
                b["cpu_remaining"] -= pod.cpu_request
                b["mem_remaining"] -= pod.memory_request
                placed = True
                break
        if not placed:
            return False
    return True


def compute_taints(node_states: dict[str, NodeState], cfg: Config) -> tuple[set[str], set[str], set[str]]:
    """Decide which nodes to taint and which to untaint.

    Returns (nodes_to_taint, nodes_to_untaint, mandatory_untaint).

    mandatory_untaint is a subset of nodes_to_untaint that must be
    untainted regardless of cooldown — these are min_nodes enforcement
    untaints (a safety invariant, not a preference).
    """
    if not node_states:
        return set(), set(), set()

    pools: dict[str, list[NodeState]] = defaultdict(list)
    for ns in node_states.values():
        pools[ns.nodepool].append(ns)

    to_taint: set[str] = set()
    to_untaint: set[str] = set()
    mandatory_untaint: set[str] = set()

    for _pool_name, pool_nodes in pools.items():
        all_workload_pods = []
        for node in pool_nodes:
            all_workload_pods.extend(node.workload_pods)

        min_needed = bin_pack_min_nodes(all_workload_pods, pool_nodes)
        min_needed = max(min_needed, cfg.min_nodes)

        surplus = len(pool_nodes) - min_needed
        if surplus <= 0:
            # All nodes are needed — untaint any that are tainted.
            # This is a min_nodes enforcement: mandatory.
            for node in pool_nodes:
                if node.is_tainted:
                    to_untaint.add(node.name)
                    mandatory_untaint.add(node.name)
            continue

        # Priority: old nodes first, then lowest utilization, then youngest
        # pod age descending (nodes whose youngest pod is oldest are closer
        # to draining naturally -- higher age = better taint candidate)
        def taint_priority(node: NodeState) -> tuple:
            is_old = 1 if node.uptime_hours > cfg.max_uptime_hours else 0
            return (
                -is_old,
                node.utilization,
                -node.youngest_pod_age_seconds,
            )

        candidates = sorted(pool_nodes, key=taint_priority)

        # Nodes beyond the surplus count are definitely remaining untainted.
        # Nodes within the surplus range that fail the safety check also
        # become remaining. This avoids the stale-snapshot bug where
        # remaining_untainted was pre-computed before the loop.
        definitely_remaining = list(candidates[surplus:])
        conditionally_remaining: list[NodeState] = []

        taint_count = 0
        for node in candidates:
            if taint_count >= surplus:
                # Already past surplus -- this node stays untainted.
                # This is min_nodes enforcement: mandatory.
                if node.is_tainted:
                    to_untaint.add(node.name)
                    mandatory_untaint.add(node.name)
                continue

            # This node is a taint candidate -- check safety
            all_remaining = definitely_remaining + conditionally_remaining
            if node.workload_pods and not _pods_fit_on_nodes(node.workload_pods, all_remaining):
                log.info(
                    "Skipping taint of %s: pods cannot fit on remaining nodes",
                    node.name,
                )
                conditionally_remaining.append(node)
                if node.is_tainted:
                    to_untaint.add(node.name)
                continue

            to_taint.add(node.name)
            taint_count += 1

    return to_taint, to_untaint, mandatory_untaint
