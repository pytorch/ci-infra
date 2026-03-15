"""Taint management and pending pod detection for the Node Compactor."""

import logging

from lightkube import ApiError, Client
from lightkube.resources.core_v1 import Node
from lightkube.types import PatchType
from models import Config, NodeState, pod_cpu_request, pod_memory_request

log = logging.getLogger("compactor")


def _pod_matches_node(pod, node: NodeState, node_taints: list) -> bool:
    """Check if a pending pod could run on a given node.

    Performs basic matching: checks that the pod tolerates the node's
    instance-type taint (all runner nodes have one). Full scheduler
    simulation is not attempted.
    """
    # Collect pod tolerations
    tolerations = []
    if pod.spec and pod.spec.tolerations:
        tolerations = pod.spec.tolerations

    # Check that the pod tolerates all node taints
    # (nodeSelector matching is skipped -- NodeState doesn't carry labels)
    for taint in node_taints:
        tolerated = False
        for tol in tolerations:
            # A toleration matches if the key matches (or key is empty with Exists operator)
            if tol.operator == "Exists" and not tol.key:
                tolerated = True
                break
            if tol.key == taint.key:
                if tol.operator == "Exists":
                    tolerated = True
                    break
                if tol.effect and tol.effect != taint.effect:
                    continue
                if getattr(tol, "value", None) == getattr(taint, "value", None):
                    tolerated = True
                    break
        if not tolerated:
            return False

    return True


def check_pending_pods(cfg: Config, node_states: dict[str, NodeState], pending_pods: list) -> set[str]:
    """Check for unschedulable pending pods; return nodes to untaint.

    If pending pods exist that could run on tainted nodes, untaint enough
    nodes (sorted by highest utilization first) to absorb the total
    resource demand of the compatible pending pods.

    Uses pre-collected pending pods and node taint data from node_states
    to avoid redundant API calls.
    """
    if not pending_pods:
        return set()

    tainted_nodes = [ns for ns in node_states.values() if ns.is_tainted]
    if not tainted_nodes:
        return set()

    # Build taint map from node_states (excluding our compactor taint)
    node_taints_map: dict[str, list] = {}
    for tnode in tainted_nodes:
        node_taints_map[tnode.name] = [t for t in tnode.node_taints if t.key != cfg.taint_key]

    # Only count demand from pods that actually match compatible tainted nodes
    compatible_pending = []
    for pod in pending_pods:
        for tnode in tainted_nodes:
            remaining_taints = node_taints_map.get(tnode.name, [])
            if _pod_matches_node(pod, tnode, remaining_taints):
                compatible_pending.append(pod)
                break

    if not compatible_pending:
        log.debug("Pending pods found but none match tainted nodes")
        return set()

    # Filter tainted nodes to those that pending pods could actually run on
    compatible_tainted: list[NodeState] = []
    for tnode in tainted_nodes:
        remaining_taints = node_taints_map.get(tnode.name, [])
        if any(_pod_matches_node(pod, tnode, remaining_taints) for pod in compatible_pending):
            compatible_tainted.append(tnode)

    if not compatible_tainted:
        return set()

    # Calculate total resource demand of compatible pending pods only
    total_cpu_demand = sum(pod_cpu_request(pod) for pod in compatible_pending)
    total_mem_demand = sum(pod_memory_request(pod) for pod in compatible_pending)

    # Sort by highest utilization first (least wasteful to untaint)
    compatible_tainted.sort(key=lambda n: n.utilization, reverse=True)

    # Untaint enough nodes to cover the demand
    nodes_to_untaint: set[str] = set()
    cumulative_cpu = 0.0
    cumulative_mem = 0

    for tnode in compatible_tainted:
        nodes_to_untaint.add(tnode.name)
        # Available capacity = allocatable minus currently used
        cumulative_cpu += tnode.allocatable_cpu - tnode.cpu_used
        cumulative_mem += tnode.allocatable_memory - tnode.memory_used

        if cumulative_cpu >= total_cpu_demand and cumulative_mem >= total_mem_demand:
            break

    log.info(
        "Found %d compatible pending pod(s) (%.1f CPU, %d MiB), untainting %d node(s) to absorb: %s",
        len(compatible_pending),
        total_cpu_demand,
        total_mem_demand // (1024 * 1024),
        len(nodes_to_untaint),
        ", ".join(sorted(nodes_to_untaint)),
    )
    return nodes_to_untaint


def apply_taint(client: Client, node_name: str, taint_key: str, dry_run: bool) -> None:
    """Add NoSchedule taint to a node."""
    patch = {
        "spec": {
            "taints": [
                {
                    "key": taint_key,
                    "value": "true",
                    "effect": "NoSchedule",
                }
            ]
        }
    }
    if dry_run:
        log.info("[DRY RUN] Would taint node %s", node_name)
        return

    client.patch(Node, node_name, patch, patch_type=PatchType.STRATEGIC)
    log.info("Tainted node %s", node_name)


def remove_taint(client: Client, node_name: str, taint_key: str, dry_run: bool, max_retries: int = 3) -> None:
    """Remove our compactor taint from a node.

    Uses optimistic concurrency via resourceVersion in a JSON merge patch.
    The Kubernetes API server enforces resourceVersion when included in
    the patch metadata, rejecting the patch with 409 Conflict if the node
    was modified since we read it. This prevents TOCTOU races.

    Retries on 409 Conflict (concurrent modification).
    """
    if dry_run:
        log.info("[DRY RUN] Would untaint node %s", node_name)
        return

    for attempt in range(max_retries):
        node = client.get(Node, node_name)
        taints = node.spec.taints or [] if node.spec else []
        new_taints = [t for t in taints if t.key != taint_key]

        patch = {
            "metadata": {"resourceVersion": node.metadata.resourceVersion},
            "spec": {"taints": new_taints or None},
        }
        try:
            client.patch(Node, node_name, patch, patch_type=PatchType.MERGE)
            log.info("Untainted node %s", node_name)
            return
        except ApiError as e:
            if e.status.code == 409 and attempt < max_retries - 1:
                log.warning(
                    "Conflict untainting %s (attempt %d/%d), retrying",
                    node_name,
                    attempt + 1,
                    max_retries,
                )
                continue
            raise


def cleanup_stale_taints(client: Client, cfg: Config) -> None:
    """Remove compactor taints from all nodes.

    Called on graceful shutdown to ensure a clean state.
    """
    log.info("Cleaning up compactor taints...")
    count = 0
    for node in client.list(Node):
        if not node.spec or not node.spec.taints:
            continue
        has_our_taint = any(t.key == cfg.taint_key for t in node.spec.taints)
        if has_our_taint:
            remove_taint(client, node.metadata.name, cfg.taint_key, dry_run=False)
            count += 1

    if count:
        log.info("Removed stale taints from %d node(s)", count)
    else:
        log.info("No stale taints found")
