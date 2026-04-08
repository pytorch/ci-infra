"""Node discovery and state building for the Node Compactor."""

import logging
from datetime import UTC, datetime

from lightkube import ApiError, Client
from lightkube.resources.core_v1 import Namespace, Node, Pod
from models import (
    ANNOTATION_CAPACITY_RESERVED,
    Config,
    NodeState,
    PodInfo,
    is_daemonset_pod,
    parse_cpu,
    parse_memory,
    pod_cpu_request,
    pod_memory_request,
)

# Minimum age (seconds) before a pending pod is considered for burst detection.
# Pods younger than this are likely still being scheduled normally.
PENDING_POD_MIN_AGE_SECONDS = 30

log = logging.getLogger("compactor")


def discover_managed_nodes(client: Client, cfg: Config) -> dict[str, str]:
    """Find nodes belonging to NodePools labeled for compaction.

    Karpenter labels nodes with karpenter.sh/nodepool=<name>. We list
    NodePools with our label, then find nodes belonging to those pools.

    Returns:
        dict mapping node_name -> pool_name. Empty dict if Karpenter
        CRDs are not installed or no managed pools exist.
    """
    from lightkube.generic_resource import create_global_resource

    try:
        nodepool_resource = create_global_resource(
            group="karpenter.sh", version="v1", kind="NodePool", plural="nodepools"
        )
        managed_pools: set[str] = set()
        for np in client.list(nodepool_resource):
            labels = (np.metadata and np.metadata.labels) or {}
            if labels.get(cfg.nodepool_label) == "true":
                managed_pools.add(np.metadata.name)
    except ApiError as e:
        if e.status.code == 404:
            log.warning(
                "Karpenter CRDs not found (NodePool resource not registered). "
                "Ensure Karpenter is installed. No nodes will be managed."
            )
            return {}
        raise

    if not managed_pools:
        return {}

    managed_nodes: dict[str, str] = {}
    for node in client.list(Node):
        labels = (node.metadata and node.metadata.labels) or {}
        pool = labels.get("karpenter.sh/nodepool", "")
        if pool in managed_pools:
            managed_nodes[node.metadata.name] = pool

    return managed_nodes


def build_node_states(
    client: Client, cfg: Config, managed_node_names: dict[str, str]
) -> tuple[dict[str, NodeState], list]:
    """Build NodeState for each managed node with its pods.

    Args:
        managed_node_names: dict mapping node_name -> pool_name from
            discover_managed_nodes().

    Also returns raw pending pod objects (unschedulable) for burst
    absorption checks, avoiding a redundant API call.
    """
    if not managed_node_names:
        return {}, []

    managed_set = set(managed_node_names)
    node_states: dict[str, NodeState] = {}

    for node in client.list(Node):
        name = node.metadata.name
        if name not in managed_set:
            continue

        labels = (node.metadata and node.metadata.labels) or {}
        annotations = (node.metadata and node.metadata.annotations) or {}
        alloc = node.status.allocatable or {} if node.status else {}
        creation = node.metadata.creationTimestamp if node.metadata else None
        if not creation:
            creation = datetime.now(UTC)

        taints = node.spec.taints or [] if node.spec else []
        is_tainted = any(t.key == cfg.taint_key for t in taints)
        is_reserved = annotations.get(ANNOTATION_CAPACITY_RESERVED) == "true"

        node_states[name] = NodeState(
            name=name,
            nodepool=labels.get("karpenter.sh/nodepool", "unknown"),
            allocatable_cpu=parse_cpu(alloc.get("cpu", "0")),
            allocatable_memory=parse_memory(alloc.get("memory", "0")),
            creation_time=creation,
            is_tainted=is_tainted,
            is_reserved=is_reserved,
            node_taints=list(taints),
            labels=dict(labels),
            annotations=dict(annotations),
        )

    # Build a set of terminating namespaces to exclude pending pods from.
    # We do a single list call rather than per-pod lookups.
    terminating_namespaces: set[str] = set()
    for ns in client.list(Namespace):
        if ns.metadata and ns.metadata.deletionTimestamp:
            terminating_namespaces.add(ns.metadata.name)

    pending_pods = []
    now = datetime.now(UTC)
    unschedulable_count = 0
    other_pending_count = 0

    for pod in client.list(Pod, namespace="*"):
        phase = pod.status.phase if pod.status else None

        # Collect pending pods without a nodeName for burst absorption.
        # Apply exclusion filters to avoid noise from pods that aren't
        # genuinely waiting for capacity.
        if phase == "Pending":
            # Only consider pods not yet assigned to a node
            if pod.spec and pod.spec.nodeName:
                continue

            # Skip Terminating pods — resources will be freed imminently
            if pod.metadata and pod.metadata.deletionTimestamp:
                continue

            # --- Exclusion filter 1: scheduling gates ---
            scheduling_gates = getattr(pod.spec, "schedulingGates", None) if pod.spec else None
            if scheduling_gates:
                continue

            # --- Exclusion filter 2: DaemonSet-owned pods ---
            if is_daemonset_pod(pod):
                continue

            # --- Exclusion filter 3: pod too young (< 30s) ---
            creation_ts = pod.metadata.creationTimestamp if pod.metadata else None
            if creation_ts:
                age = (now - creation_ts).total_seconds()
                if age < PENDING_POD_MIN_AGE_SECONDS:
                    continue

            # --- Exclusion filter 4: terminating namespace ---
            pod_ns = pod.metadata.namespace if pod.metadata else None
            if pod_ns and pod_ns in terminating_namespaces:
                continue

            # --- Exclusion filter 5: waiting for volume binding ---
            conditions = pod.status.conditions or [] if pod.status else []
            is_volume_wait = any(
                c.type == "PodScheduled"
                and c.status == "False"
                and c.message
                and ("persistentvolumeclaim" in c.message.lower() or "bound" in c.message.lower())
                for c in conditions
            )
            if is_volume_wait:
                continue

            # Track unschedulable vs other pending for logging
            is_unschedulable = any(
                c.reason == "Unschedulable" and c.status == "False" for c in conditions if c.type == "PodScheduled"
            )
            if is_unschedulable:
                unschedulable_count += 1
            else:
                other_pending_count += 1

            pending_pods.append(pod)
            continue

        if not pod.spec or not pod.spec.nodeName:
            continue
        node_name = pod.spec.nodeName
        if node_name not in node_states:
            continue
        if phase in ("Succeeded", "Failed"):
            continue
        # Skip Terminating pods — resources will be freed imminently
        if pod.metadata and pod.metadata.deletionTimestamp:
            continue

        start_time = None
        if pod.status and pod.status.startTime:
            start_time = pod.status.startTime

        node_states[node_name].pods.append(
            PodInfo(
                name=pod.metadata.name,
                namespace=pod.metadata.namespace,
                cpu_request=pod_cpu_request(pod),
                memory_request=pod_memory_request(pod),
                node_name=node_name,
                is_daemonset=is_daemonset_pod(pod),
                start_time=start_time,
            )
        )

    if pending_pods:
        log.info(
            "Pending pods: %d total (%d unschedulable, %d other pending)",
            len(pending_pods),
            unschedulable_count,
            other_pending_count,
        )

    return node_states, pending_pods
