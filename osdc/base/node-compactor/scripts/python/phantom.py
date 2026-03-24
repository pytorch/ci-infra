"""Phantom load simulation for the Node Compactor.

Counts pending pods toward target node utilization by simulating their
placement on compatible untainted nodes. This prevents the compactor from
tainting nodes that are about to receive real workload.
"""

import logging
from datetime import UTC, datetime

import metrics as m
from models import Config, NodeState, PodInfo, pod_cpu_request, pod_memory_request
from taints import _pod_matches_node

log = logging.getLogger("compactor")

# Maximum age (seconds) for a pending pod to be eligible for phantom placement.
# Pods pending longer than this are considered stale — our prediction is likely wrong.
PHANTOM_MAX_PENDING_SECONDS = 120

# Minimum age (seconds) for a pending pod to be eligible for phantom placement.
# Pods younger than this are still being scheduled normally.
PHANTOM_MIN_PENDING_SECONDS = 30

# Maximum fraction of a node's allocatable resources that phantom pods may consume.
# Prevents phantom inflation from freezing compaction decisions.
PHANTOM_LOAD_CAP = 0.3


def apply_pending_phantom_load(
    node_states: dict[str, NodeState],
    pending_pods: list,
    cfg: Config,
) -> None:
    """Simulate pending pod placement as phantom load on nodes.

    For each eligible pending pod, find the least-utilized compatible untainted
    node and add a phantom PodInfo to it. This makes the compactor's utilization
    calculation account for pods that are about to land, preventing premature
    tainting of nodes that will soon be needed.

    Modifies node_states in-place by appending phantom PodInfo to node pods lists.
    """
    if not pending_pods or not node_states:
        return

    now = datetime.now(UTC)

    # Filter to untainted nodes only — tainted nodes won't receive new pods
    untainted_nodes = [ns for ns in node_states.values() if not ns.is_tainted]
    if not untainted_nodes:
        return

    # Track phantom CPU/memory per node to enforce the cap
    phantom_cpu: dict[str, float] = {}
    phantom_memory: dict[str, int] = {}

    placed_count = 0
    target_nodes: set[str] = set()

    for pod in pending_pods:
        # --- Age filter ---
        creation_ts = pod.metadata.creationTimestamp if pod.metadata else None
        if creation_ts:
            age = (now - creation_ts).total_seconds()
            if age < PHANTOM_MIN_PENDING_SECONDS:
                continue
            if age > PHANTOM_MAX_PENDING_SECONDS:
                continue

        cpu_req = pod_cpu_request(pod)
        mem_req = pod_memory_request(pod)

        # Find all compatible untainted nodes
        compatible = [ns for ns in untainted_nodes if _pod_matches_node(pod, ns)]
        if not compatible:
            continue

        # Pick the node with lowest utilization (LeastAllocated strategy)
        compatible.sort(key=lambda ns: ns.utilization)

        for candidate in compatible:
            node_name = candidate.name

            # Check phantom load cap: only phantom pods count toward the 30% cap
            current_phantom_cpu = phantom_cpu.get(node_name, 0.0)
            current_phantom_mem = phantom_memory.get(node_name, 0)

            if candidate.allocatable_cpu > 0:
                phantom_cpu_ratio = (current_phantom_cpu + cpu_req) / candidate.allocatable_cpu
                if phantom_cpu_ratio > PHANTOM_LOAD_CAP:
                    continue

            if candidate.allocatable_memory > 0:
                phantom_mem_ratio = (current_phantom_mem + mem_req) / candidate.allocatable_memory
                if phantom_mem_ratio > PHANTOM_LOAD_CAP:
                    continue

            # Place the phantom pod
            pod_name = f"phantom-{pod.metadata.name}" if pod.metadata else f"phantom-{placed_count}"
            pod_ns = pod.metadata.namespace if pod.metadata else "unknown"

            phantom_pod = PodInfo(
                name=pod_name,
                namespace=pod_ns,
                cpu_request=cpu_req,
                memory_request=mem_req,
                node_name=node_name,
                is_daemonset=False,
                is_phantom=True,
            )
            candidate.pods.append(phantom_pod)

            phantom_cpu[node_name] = current_phantom_cpu + cpu_req
            phantom_memory[node_name] = current_phantom_mem + mem_req

            placed_count += 1
            target_nodes.add(node_name)
            log.debug(
                "Phantom placement: %s -> %s (cpu=%.2f, mem=%d MiB)",
                pod_name,
                node_name,
                cpu_req,
                mem_req // (1024 * 1024),
            )
            break

    if placed_count:
        log.info("Applied phantom load: %d pods across %d nodes", placed_count, len(target_nodes))

    # Emit per-pool phantom load metrics
    pool_phantom: dict[str, int] = {}
    for ns in node_states.values():
        count = sum(1 for p in ns.pods if p.is_phantom)
        if count:
            pool_phantom[ns.nodepool] = pool_phantom.get(ns.nodepool, 0) + count
    m.refresh_gauge(
        m.phantom_load_pods,
        {(pool,): count for pool, count in pool_phantom.items()},
    )
