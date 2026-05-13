"""Node-state utilities — stability classification, age tracking, taint filtering."""

from __future__ import annotations

import time

MIN_NODE_AGE_SECONDS = 600  # Nodes must be Ready for 10+ min to count as stable
RECENTLY_STABLE_AGE_SECONDS = 1200  # Nodes < 20 min may still have DaemonSet pods starting

# Taint keys signalling the node is unstable — about to be terminated, or
# already known to be unhealthy by the kubelet/node-controller. The first
# two are set by cluster-level controllers (Karpenter consolidating/expiring,
# OSDC node-compactor draining) while the kubelet still reports Ready=True
# for a brief window. The remaining three are well-known K8s lifecycle taints
# that the node-controller / kubelet apply when a node is unreachable or
# being marked out-of-service. DaemonSet pods on any of these will be killed
# (or have already been rejected) by the kubelet's NodeShutdown admission, so
# they shouldn't count as "should host healthy pods right now" in smoke tests.
_DISRUPTION_TAINT_KEYS: frozenset[str] = frozenset(
    {
        "karpenter.sh/disrupted",
        "node-compactor.osdc.io/consolidating",
        "node.kubernetes.io/unreachable",
        "node.kubernetes.io/not-ready",
        "node.kubernetes.io/out-of-service",
    }
)

__all__ = [
    "MIN_NODE_AGE_SECONDS",
    "RECENTLY_STABLE_AGE_SECONDS",
    "_DISRUPTION_TAINT_KEYS",
    "_count_unstable_nodes",
    "_has_matching_nodes",
    "_is_node_unstable",
    "_parse_k8s_timestamp",
    "get_all_node_names",
    "get_recently_stable_node_names",
    "get_unstable_node_names",
    "pod_age_seconds",
    "pod_is_on_unstable_node",
]


def _parse_k8s_timestamp(ts: str) -> float:
    """Parse Kubernetes ISO 8601 timestamp to Unix epoch seconds."""
    from datetime import datetime

    return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()


def pod_age_seconds(pod: dict) -> float | None:
    """Pod age in seconds from metadata.creationTimestamp, or None if missing.

    Use to grant a startup window to freshly-(re)scheduled pods independent
    of node age — DaemonSets can roll/evict onto long-stable nodes, and the
    new pod needs the same image-pull/init time as one on a young node.
    """
    created = pod.get("metadata", {}).get("creationTimestamp", "")
    if not created:
        return None
    return time.time() - _parse_k8s_timestamp(created)


def _is_node_unstable(node: dict, min_node_age: int = MIN_NODE_AGE_SECONDS) -> bool:
    """Check if a single node is unstable (new, NotReady, cordoned, deleting, or being disrupted)."""
    meta = node.get("metadata", {})
    if meta.get("deletionTimestamp"):
        return True
    if node.get("spec", {}).get("unschedulable"):
        return True
    for taint in node.get("spec", {}).get("taints", []) or []:
        if taint.get("key") in _DISRUPTION_TAINT_KEYS:
            return True
    conditions = {c["type"]: c["status"] for c in node.get("status", {}).get("conditions", [])}
    if conditions.get("Ready") != "True":
        return True
    created = meta.get("creationTimestamp", "")
    if created:
        created_ts = _parse_k8s_timestamp(created)
        if (time.time() - created_ts) < min_node_age:
            return True
    return False


def _count_unstable_nodes(all_nodes: dict, min_node_age: int = MIN_NODE_AGE_SECONDS) -> int:
    """Count nodes that are new, NotReady, cordoned, or being deleted.

    A node is "unstable" if any of:
    - It has a deletionTimestamp (being deleted)
    - It is cordoned (spec.unschedulable is true)
    - It carries a disruption taint (Karpenter / node-compactor draining)
    - Its Ready condition is not True
    - It was created less than min_node_age seconds ago
    """
    return sum(1 for node in all_nodes.get("items", []) if _is_node_unstable(node, min_node_age=min_node_age))


def get_unstable_node_names(all_nodes: dict, min_node_age: int = MIN_NODE_AGE_SECONDS) -> set[str]:
    """Return names of nodes that are new, NotReady, cordoned, being deleted, or being disrupted."""
    return {node["metadata"]["name"] for node in all_nodes.get("items", []) if _is_node_unstable(node, min_node_age=min_node_age)}


def pod_is_on_unstable_node(pod: dict, all_nodes: dict, min_node_age: int = MIN_NODE_AGE_SECONDS) -> bool:
    """True if the pod's host node is missing from the snapshot OR unstable.

    Handles two cases that should both be excluded from "should be running"
    accounting in smoke tests:

    1. The Node object the pod was scheduled to has been garbage-collected
       between the time we listed nodes and the time we listed pods. Karpenter
       disruption rolls produce a steady stream of such pods (DaemonSet
       controller schedules onto a dying nodeName, kubelet rejects with
       NodeShutdown, but by the time we observe the pod the Node object is
       gone — so ``get_unstable_node_names`` cannot mark it).
    2. The Node object still exists but is unstable (deleting, cordoned,
       disruption-tainted, NotReady, or too young — see ``_is_node_unstable``).

    Pods with no ``spec.nodeName`` (unscheduled — extremely rare for
    DaemonSets) are also treated as on an unstable host.
    """
    node_name = pod.get("spec", {}).get("nodeName")
    if not node_name:
        return True
    nodes_by_name = {n["metadata"]["name"]: n for n in all_nodes.get("items", [])}
    node = nodes_by_name.get(node_name)
    if node is None:
        return True
    return _is_node_unstable(node, min_node_age=min_node_age)


def get_all_node_names(all_nodes: dict) -> set[str]:
    """Return names of all nodes currently known to the API server."""
    return {node["metadata"]["name"] for node in all_nodes.get("items", [])}


def get_recently_stable_node_names(
    all_nodes: dict,
    min_node_age: int = MIN_NODE_AGE_SECONDS,
    recently_stable_age: int = RECENTLY_STABLE_AGE_SECONDS,
) -> set[str]:
    """Return names of nodes that are stable but still young.

    These nodes passed the min_node_age threshold (not in the unstable
    set) but are younger than recently_stable_age seconds. DaemonSet pods on
    these nodes may still be pulling images or creating containers — Pending
    phase is expected and should not be treated as a failure.
    """
    names = set()
    for node in all_nodes.get("items", []):
        if _is_node_unstable(node, min_node_age=min_node_age):
            continue
        meta = node.get("metadata", {})
        created = meta.get("creationTimestamp", "")
        if created:
            age = time.time() - _parse_k8s_timestamp(created)
            if age < recently_stable_age:
                names.add(meta["name"])
    return names


def _has_matching_nodes(all_nodes: dict, node_selector: dict[str, list[str]] | None) -> bool:
    """Check if any nodes match a label selector.

    When a DaemonSet targets specific nodes via nodeAffinity, desired can
    legitimately be 0 when no matching nodes exist (e.g. Karpenter scaled
    runner/buildkit pools to zero). Returns True if no selector is given
    (conservative — assume matching nodes exist).
    """
    if node_selector is None:
        return True
    for node in all_nodes.get("items", []):
        node_labels = node.get("metadata", {}).get("labels", {})
        if all(node_labels.get(k) in vs for k, vs in node_selector.items()):
            return True
    return False
