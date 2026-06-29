"""Bin-packing and taint computation for the Node Compactor."""

import logging
import math
from collections import defaultdict
from collections.abc import Callable

from fit import _pods_fit_on_nodes
from models import Config, NodeState, PodInfo
from peak_window import prune_stale_peak_history, update_peak_history
from pending import pending_pods_for_group

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

    sorted_pods = sorted(pods, key=lambda p: (p.gpu_request, p.cpu_request), reverse=True)

    bins: list[dict] = []
    for node in sorted(nodes, key=lambda n: n.allocatable_cpu, reverse=True):
        bins.append(
            {
                "cpu_remaining": node.allocatable_cpu - node.daemonset_cpu,
                "mem_remaining": node.allocatable_memory - node.daemonset_memory,
                "gpu_remaining": node.allocatable_gpu - node.daemonset_gpu,
            }
        )

    nodes_used = 0
    for pod in sorted_pods:
        placed = False
        for b in bins[:nodes_used]:
            if (
                b["cpu_remaining"] >= pod.cpu_request
                and b["mem_remaining"] >= pod.memory_request
                and (pod.gpu_request == 0 or b["gpu_remaining"] >= pod.gpu_request)
            ):
                b["cpu_remaining"] -= pod.cpu_request
                b["mem_remaining"] -= pod.memory_request
                b["gpu_remaining"] -= pod.gpu_request
                placed = True
                break
        if not placed:
            if nodes_used < len(bins):
                b = bins[nodes_used]
                b["cpu_remaining"] -= pod.cpu_request
                b["mem_remaining"] -= pod.memory_request
                b["gpu_remaining"] -= pod.gpu_request
                nodes_used += 1
            else:
                return len(bins)

    return max(nodes_used, 1)


def select_reserved_nodes(group_nodes: dict[str, list[NodeState]], cfg: Config) -> dict[str, set[str]]:
    """Select nodes per pool for capacity reservation (do-not-disrupt).

    Only young nodes (< max_uptime_hours) are eligible. Selection priority:
    lowest utilization first (ready-to-use capacity), then oldest youngest-pod
    age (closer to draining), then newest node as tiebreaker.

    Accepts any grouping (e.g. fleet-grouped data) but internally re-groups by
    NodePool, since reservations are per-NodePool.

    Returns {pool_name: {node_names}} with up to cfg.capacity_reservation_nodes per pool.
    """
    if cfg.capacity_reservation_nodes <= 0:
        return {}
    # Reservations are per-NodePool — re-group internally regardless of caller grouping
    pool_nodes: dict[str, list[NodeState]] = defaultdict(list)
    for nodes in group_nodes.values():
        for ns in nodes:
            pool_nodes[ns.nodepool].append(ns)

    result: dict[str, set[str]] = {}
    for pool_name, nodes in pool_nodes.items():
        young = [n for n in nodes if n.uptime_hours < cfg.max_uptime_hours]
        if not young:
            log.info(
                "Reservation: pool %s has %d node(s) but none younger than %dh",
                pool_name,
                len(nodes),
                cfg.max_uptime_hours,
            )
            continue

        # Sort: lowest utilization, then oldest youngest-pod (closer to
        # draining), then newest node (most recently provisioned = freshest)
        young.sort(
            key=lambda n: (
                n.utilization,
                -n.youngest_pod_age_seconds,
                -n.uptime_seconds,
            )
        )

        selected = {n.name for n in young[: cfg.capacity_reservation_nodes]}
        if selected:
            result[pool_name] = selected
            log.info(
                "Reservation: pool %s — selected %s (from %d eligible, %d total)",
                pool_name,
                sorted(selected),
                len(young),
                len(nodes),
            )

    return result


def compute_taints(
    node_states: dict[str, NodeState],
    cfg: Config,
    reserved_nodes: set[str] | None = None,
    group_key: Callable[[NodeState], str] | None = None,
    peak_history: dict[str, list[tuple[float, int]]] | None = None,
    pending_pods: list | None = None,
) -> tuple[set[str], set[str], set[str], set[str]]:
    """Decide which nodes to taint and which to untaint.

    Returns (nodes_to_taint, nodes_to_untaint, mandatory_untaint, rate_limited).

    mandatory_untaint is a subset of nodes_to_untaint that must be
    untainted regardless of cooldown — these are min_nodes enforcement
    untaints (a safety invariant, not a preference).

    rate_limited contains nodes that would have been tainted but were
    blocked by the per-iteration taint rate limit (cfg.taint_rate).
    """
    if not node_states:
        return set(), set(), set(), set()

    key_fn = group_key or (lambda ns: ns.nodepool)

    groups: dict[str, list[NodeState]] = defaultdict(list)
    for ns in node_states.values():
        groups[key_fn(ns)].append(ns)

    if peak_history is not None:
        prune_stale_peak_history(peak_history)

    to_taint: set[str] = set()
    to_untaint: set[str] = set()
    mandatory_untaint: set[str] = set()
    rate_limited: set[str] = set()

    reserved = reserved_nodes or set()

    for group_name, group_nodes in groups.items():
        all_workload_pods = []
        for node in group_nodes:
            all_workload_pods.extend(p for p in node.workload_pods if not p.is_phantom)

        if pending_pods:
            all_workload_pods.extend(pending_pods_for_group(pending_pods, group_nodes, cfg.taint_key))

        current_min = bin_pack_min_nodes(all_workload_pods, group_nodes)

        if peak_history is not None:
            peak_min = update_peak_history(peak_history, group_name, current_min, cfg.interval)
        else:
            peak_min = current_min

        min_needed = max(peak_min, cfg.min_nodes)

        surplus = len(group_nodes) - min_needed
        if surplus <= 0:
            # All nodes are needed — untaint any that are tainted.
            # This is a min_nodes enforcement: mandatory.
            for node in group_nodes:
                if node.is_tainted:
                    to_untaint.add(node.name)
                    mandatory_untaint.add(node.name)
            continue

        # Compute required spare capacity for this group. spare_capacity_nodes
        # is a floor, spare_capacity_ratio scales with group size. The effective
        # requirement is the max of both. Setting both to 0 disables the feature.
        required_spare = max(
            cfg.spare_capacity_nodes,
            math.ceil(len(group_nodes) * cfg.spare_capacity_ratio),
        )

        # Exclude young nodes and reserved nodes from taint candidates. Young
        # nodes may not have received pods yet (race between Karpenter provisioning
        # and the compactor's reconcile cycle). Reserved nodes are protected
        # from disruption to maintain ready-to-use capacity.
        eligible = []
        for node in group_nodes:
            if node.name in reserved:
                log.debug("Skipping %s: capacity-reserved", node.name)
                if node.is_tainted:
                    to_untaint.add(node.name)
                    mandatory_untaint.add(node.name)
                continue
            if node.uptime_seconds < cfg.min_node_age:
                log.debug("Skipping %s: too young (%.0fs < %ds)", node.name, node.uptime_seconds, cfg.min_node_age)
                if node.is_tainted:
                    to_untaint.add(node.name)
                    mandatory_untaint.add(node.name)
                continue
            eligible.append(node)

        # Priority: old nodes first, then lowest utilization, then youngest pod
        # age descending (nodes whose youngest pod is oldest are closer to
        # draining naturally -- higher age = better taint candidate)
        def taint_priority(node: NodeState) -> tuple:
            is_old = 1 if node.uptime_hours > cfg.max_uptime_hours else 0
            return (
                -is_old,
                node.allocatable_cpu,
                node.allocatable_gpu,
                node.utilization,
                -node.youngest_pod_age_seconds,
            )

        candidates = sorted(eligible, key=taint_priority)

        # Rate limit: cap the number of NEW taints (nodes not already tainted)
        # per iteration to avoid large-scale taint storms. Always allow at least
        # 1 new taint even with rate=0.0.
        max_new_taints = max(1, math.ceil(surplus * cfg.taint_rate))
        new_taint_count = 0

        # Nodes beyond the surplus count are definitely remaining untainted.
        # Nodes within the surplus range that fail the safety check also become
        # remaining. This avoids the stale-snapshot bug where remaining_untainted
        # was pre-computed before the loop.
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

            # This node is a taint candidate -- check safety. Exclude phantom
            # pods: they are predictions about pending pod placement, not real
            # workload that needs to be accommodated.
            real_pods = [p for p in node.workload_pods if not p.is_phantom]
            all_remaining = definitely_remaining + conditionally_remaining
            if real_pods and not _pods_fit_on_nodes(real_pods, all_remaining):
                log.info(
                    "Skipping taint of %s: pods cannot fit on remaining nodes",
                    node.name,
                )
                conditionally_remaining.append(node)
                if node.is_tainted:
                    to_untaint.add(node.name)
                continue

            # Rate limit: already-tainted nodes don't count toward the cap
            # (they maintain existing state). Only new taints are limited.
            if not node.is_tainted and new_taint_count >= max_new_taints:
                log.info(
                    "Rate-limited taint of %s: %d/%d new taints this iteration",
                    node.name,
                    new_taint_count,
                    max_new_taints,
                )
                rate_limited.add(node.name)
                conditionally_remaining.append(node)
                continue

            # Spare capacity check: ensure enough low-utilization nodes remain
            # untainted after this taint. Count untainted nodes (excluding this
            # candidate and nodes already marked to taint) with utilization at
            # or below the threshold.
            if required_spare > 0:
                spare_after = _count_spare_nodes(
                    group_nodes,
                    node.name,
                    to_taint,
                    cfg.spare_capacity_threshold,
                )
                if spare_after < required_spare:
                    log.info(
                        "Skipping taint of %s: would violate spare capacity (spare_after=%d < required=%d)",
                        node.name,
                        spare_after,
                        required_spare,
                    )
                    conditionally_remaining.append(node)
                    if node.is_tainted:
                        to_untaint.add(node.name)
                    continue

            to_taint.add(node.name)
            taint_count += 1
            if not node.is_tainted:
                new_taint_count += 1

        # Spare capacity recovery: untaint low-utilization tainted nodes if the group misses the spare capacity requirement.
        if required_spare > 0:
            current_spare = _count_spare_nodes(
                group_nodes,
                None,
                to_taint,
                cfg.spare_capacity_threshold,
            )
            if current_spare < required_spare:
                # Find tainted nodes with low utilization that we can untaint
                # to restore spare capacity. Prefer lowest utilization first.
                tainted_low = sorted(
                    (
                        n
                        for n in group_nodes
                        if n.is_tainted and n.name not in to_untaint and n.utilization <= cfg.spare_capacity_threshold
                    ),
                    key=lambda n: n.utilization,
                )
                for node in tainted_low:
                    if current_spare >= required_spare:
                        break
                    to_untaint.add(node.name)
                    mandatory_untaint.add(node.name)
                    to_taint.discard(node.name)
                    current_spare += 1

    return to_taint, to_untaint, mandatory_untaint, rate_limited


def _count_spare_nodes(
    pool_nodes: list[NodeState],
    exclude_node: str | None,
    to_taint: set[str],
    threshold: float,
) -> int:
    """Count untainted nodes with utilization at or below threshold.

    Nodes in to_taint or matching exclude_node are treated as tainted.
    """
    count = 0
    for n in pool_nodes:
        if n.name == exclude_node:
            continue
        if n.is_tainted or n.name in to_taint:
            continue
        if n.utilization <= threshold:
            count += 1
    return count
