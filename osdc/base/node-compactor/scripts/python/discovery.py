"""Node discovery and state building for the Node Compactor."""

import logging
from datetime import datetime, timezone

from lightkube import ApiError, Client
from lightkube.resources.core_v1 import Node, Pod

from models import (
    Config,
    NodeState,
    PodInfo,
    is_daemonset_pod,
    parse_cpu,
    parse_memory,
    pod_cpu_request,
    pod_memory_request,
)

log = logging.getLogger("compactor")


def discover_managed_nodes(client: Client, cfg: Config) -> list[str]:
    """Find node names belonging to NodePools labeled for compaction.

    Karpenter labels nodes with karpenter.sh/nodepool=<name>. We list
    NodePools with our label, then find nodes belonging to those pools.

    Returns an empty list if Karpenter CRDs are not installed.
    """
    from lightkube.generic_resource import create_global_resource

    try:
        NodePool = create_global_resource(
            group="karpenter.sh", version="v1", kind="NodePool", plural="nodepools"
        )
        managed_pools: set[str] = set()
        for np in client.list(NodePool):
            labels = (np.metadata and np.metadata.labels) or {}
            if labels.get(cfg.nodepool_label) == "true":
                managed_pools.add(np.metadata.name)
    except ApiError as e:
        if e.status.code == 404:
            log.warning(
                "Karpenter CRDs not found (NodePool resource not registered). "
                "Ensure Karpenter is installed. No nodes will be managed."
            )
            return []
        raise

    if not managed_pools:
        return []

    managed_nodes = []
    for node in client.list(Node):
        labels = (node.metadata and node.metadata.labels) or {}
        pool = labels.get("karpenter.sh/nodepool", "")
        if pool in managed_pools:
            managed_nodes.append(node.metadata.name)

    return managed_nodes


def build_node_states(
    client: Client, cfg: Config, managed_node_names: list[str]
) -> tuple[dict[str, NodeState], list]:
    """Build NodeState for each managed node with its pods.

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
        alloc = node.status.allocatable or {} if node.status else {}
        creation = node.metadata.creationTimestamp if node.metadata else None
        if not creation:
            creation = datetime.now(timezone.utc)

        taints = node.spec.taints or [] if node.spec else []
        is_tainted = any(t.key == cfg.taint_key for t in taints)

        node_states[name] = NodeState(
            name=name,
            nodepool=labels.get("karpenter.sh/nodepool", "unknown"),
            allocatable_cpu=parse_cpu(alloc.get("cpu", "0")),
            allocatable_memory=parse_memory(alloc.get("memory", "0")),
            creation_time=creation,
            is_tainted=is_tainted,
            node_taints=list(taints),
        )

    pending_pods = []
    for pod in client.list(Pod, namespace="*"):
        phase = pod.status.phase if pod.status else None

        # Collect unschedulable pending pods for burst absorption
        if phase == "Pending":
            conditions = pod.status.conditions or [] if pod.status else []
            is_unschedulable = any(
                c.reason == "Unschedulable" and c.status == "False"
                for c in conditions
                if c.type == "PodScheduled"
            )
            if is_unschedulable:
                pending_pods.append(pod)
            continue

        if not pod.spec or not pod.spec.nodeName:
            continue
        node_name = pod.spec.nodeName
        if node_name not in node_states:
            continue
        if phase in ("Succeeded", "Failed"):
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

    return node_states, pending_pods
