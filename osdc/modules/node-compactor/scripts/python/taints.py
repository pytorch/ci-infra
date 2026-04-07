"""Taint management and pending pod detection for the Node Compactor."""

import logging
import time

from lightkube import ApiError, Client
from lightkube.resources.core_v1 import Node
from lightkube.types import PatchType
from models import (
    Config,
    NodeState,
    pod_cpu_request,
    pod_memory_request,
)

log = logging.getLogger("compactor")

# Critical taint key: Karpenter NodePools taint nodes with
# instance-type=<type>:NoSchedule. Workflow pods rely on this taint for
# scheduling constraints. If lost, pods land on wrong instance types
# (e.g., amd64 image on arm64 node → ImagePullBackOff).
INSTANCE_TYPE_TAINT_KEY = "instance-type"


def _pod_matches_node(pod, node_state: NodeState) -> bool:
    """Check if a pending pod could run on a given node.

    Checks scheduling constraints:
    1. Tolerations — pod must tolerate all node taints
    2. nodeSelector — every key=value must match node labels
    3. Node affinity (requiredDuringSchedulingIgnoredDuringExecution)
    4. Resource fit — pod requests must fit in remaining node capacity
    """
    if not pod.spec:
        return not bool(node_state.node_taints)

    # --- 1. Taint tolerations ---
    tolerations = pod.spec.tolerations or []
    for taint in node_state.node_taints:
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

    # --- 2. nodeSelector ---
    node_selector = getattr(pod.spec, "nodeSelector", None)
    if node_selector:
        for key, value in node_selector.items():
            if node_state.labels.get(key) != value:
                return False

    # --- 3. Node affinity (required only) ---
    affinity = getattr(pod.spec, "affinity", None)
    if affinity:
        node_affinity = getattr(affinity, "nodeAffinity", None)
        if node_affinity:
            required = getattr(node_affinity, "requiredDuringSchedulingIgnoredDuringExecution", None)
            if required:
                terms = getattr(required, "nodeSelectorTerms", None) or []
                if terms and not _any_term_matches(terms, node_state.labels):
                    return False

    # --- 4. Resource fit ---
    remaining_cpu = node_state.allocatable_cpu - node_state.total_cpu_used
    remaining_memory = node_state.allocatable_memory - node_state.total_memory_used
    if pod_cpu_request(pod) > remaining_cpu:
        return False
    return not pod_memory_request(pod) > remaining_memory


def _any_term_matches(terms: list, node_labels: dict) -> bool:
    """Check if any nodeSelectorTerm matches (terms are OR'd)."""
    for term in terms:
        expressions = getattr(term, "matchExpressions", None) or []
        if _all_expressions_match(expressions, node_labels):
            return True
    return False


def _all_expressions_match(expressions: list, node_labels: dict) -> bool:
    """Check if all matchExpressions in a term match (AND'd)."""
    for expr in expressions:
        key = expr.key
        operator = expr.operator
        values = getattr(expr, "values", None) or []
        node_value = node_labels.get(key)

        if operator == "In":
            if node_value is None or node_value not in values:
                return False
        elif operator == "NotIn":
            if node_value is not None and node_value in values:
                return False
        elif operator == "Exists":
            if key not in node_labels:
                return False
        elif operator == "DoesNotExist":
            if key in node_labels:
                return False
        elif operator == "Gt":
            if node_value is None:
                return False
            try:
                if int(node_value) <= int(values[0]):
                    return False
            except (ValueError, IndexError):
                return False
        elif operator == "Lt":
            if node_value is None:
                return False
            try:
                if int(node_value) >= int(values[0]):
                    return False
            except (ValueError, IndexError):
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

    # Build NodeState views with compactor taint removed (simulates untainting)
    untainted_views: dict[str, NodeState] = {}
    for tnode in tainted_nodes:
        remaining_taints = [t for t in tnode.node_taints if t.key != cfg.taint_key]
        untainted_views[tnode.name] = NodeState(
            name=tnode.name,
            nodepool=tnode.nodepool,
            allocatable_cpu=tnode.allocatable_cpu,
            allocatable_memory=tnode.allocatable_memory,
            creation_time=tnode.creation_time,
            pods=tnode.pods,
            is_tainted=tnode.is_tainted,
            node_taints=remaining_taints,
            labels=tnode.labels,
        )

    # Only count demand from pods that actually match compatible tainted nodes
    compatible_pending = []
    for pod in pending_pods:
        for tnode in tainted_nodes:
            if _pod_matches_node(pod, untainted_views[tnode.name]):
                compatible_pending.append(pod)
                break

    if not compatible_pending:
        log.debug("Pending pods found but none match tainted nodes")
        return set()

    # Filter tainted nodes to those that pending pods could actually run on
    compatible_tainted: list[NodeState] = []
    for tnode in tainted_nodes:
        if any(_pod_matches_node(pod, untainted_views[tnode.name]) for pod in compatible_pending):
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
        cumulative_cpu += tnode.allocatable_cpu - tnode.total_cpu_used
        cumulative_mem += tnode.allocatable_memory - tnode.total_memory_used

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


def apply_taint(client: Client, node_name: str, taint_key: str, dry_run: bool, max_retries: int = 5) -> None:
    """Add NoSchedule taint to a node.

    Uses RFC 6902 JSON Patch to append a single taint without replacing
    the entire taints array.  This prevents wiping critical taints
    (e.g. instance-type) that would be lost with a strategic merge patch
    on the atomic ``spec.taints`` list.

    Before patching, verifies that the node's instance-type taint (if
    expected based on labels) is present.  Retries with back-off if
    missing, aborts after *max_retries* to avoid scheduling chaos.
    """
    if dry_run:
        log.info("[DRY RUN] Would taint node %s", node_name)
        return

    for attempt in range(max_retries):
        node = client.get(Node, node_name)
        taints = node.spec.taints or [] if node.spec else []

        # Idempotent: already tainted → nothing to do
        if any(t.key == taint_key for t in taints):
            log.info("Node %s already has taint %s, skipping", node_name, taint_key)
            return

        # Guard: verify instance-type taint is present (if node expects it)
        labels = node.metadata.labels or {} if node.metadata else {}
        if labels.get(INSTANCE_TYPE_TAINT_KEY) and not any(t.key == INSTANCE_TYPE_TAINT_KEY for t in taints):
            if attempt < max_retries - 1:
                log.warning(
                    "Node %s has instance-type label but missing instance-type taint (attempt %d/%d), retrying...",
                    node_name,
                    attempt + 1,
                    max_retries,
                )
                time.sleep(2)
                continue
            log.error(
                "ABORTING taint of node %s: instance-type taint missing after %d attempts",
                node_name,
                max_retries,
            )
            return

        # RFC 6902 JSON Patch: append taint without replacing the array.
        # A ``test`` op on resourceVersion provides optimistic concurrency —
        # if another controller modified the node between our GET and PATCH,
        # the API server rejects the entire patch, and we retry with a
        # fresh read.  This closes the TOCTOU window where a concurrent
        # taint removal could go undetected.
        # When spec.taints is null/missing we must create the array
        # (appending to a null path via /- is invalid).
        rv = node.metadata.resourceVersion
        new_taint = {"key": taint_key, "value": "true", "effect": "NoSchedule"}
        rv_check = {"op": "test", "path": "/metadata/resourceVersion", "value": rv}
        if taints:
            patch = [rv_check, {"op": "add", "path": "/spec/taints/-", "value": new_taint}]
        else:
            patch = [rv_check, {"op": "add", "path": "/spec/taints", "value": [new_taint]}]

        try:
            client.patch(Node, node_name, patch, patch_type=PatchType.JSON)
            log.info("Tainted node %s", node_name)
            return
        except ApiError as e:
            if e.status.code in (409, 422) and attempt < max_retries - 1:
                log.warning(
                    "Conflict tainting %s (attempt %d/%d), retrying",
                    node_name,
                    attempt + 1,
                    max_retries,
                )
                continue
            raise


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

        # Guard: verify instance-type taint is present (if node expects it).
        # If the instance-type taint was lost (e.g., by a racing controller),
        # we must not patch the taints list — doing so would persist the loss.
        labels = node.metadata.labels or {} if node.metadata else {}
        if labels.get(INSTANCE_TYPE_TAINT_KEY) and not any(t.key == INSTANCE_TYPE_TAINT_KEY for t in taints):
            if attempt < max_retries - 1:
                log.warning(
                    "Node %s has instance-type label but missing instance-type taint (attempt %d/%d), retrying...",
                    node_name,
                    attempt + 1,
                    max_retries,
                )
                time.sleep(1)
                continue
            log.error(
                "ABORTING untaint of node %s: instance-type taint missing "
                "after %d attempts. Refusing to patch taints list.",
                node_name,
                max_retries,
            )
            return

        # Convert lightkube Taint objects to plain dicts for JSON merge
        # patch serialization (Taint objects are not JSON-serializable).
        new_taints = [
            {"key": t.key, "effect": t.effect, **({"value": t.value} if t.value else {})}
            for t in taints
            if t.key != taint_key
        ]

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
