"""Filter raw pending Pods to those eligible for bin-pack inclusion in a node group."""

from datetime import UTC, datetime

from models import (
    PENDING_POD_MAX_AGE_SECONDS,
    PENDING_POD_MIN_AGE_SECONDS,
    NodeState,
    PodInfo,
    node_view_without_taint,
    pod_cpu_request,
    pod_gpu_request,
    pod_memory_request,
    pod_to_podinfo,
)
from taints import _pod_constraints_match


def pending_pods_for_group(
    pending_pods: list,
    group_nodes: list[NodeState],
    taint_key: str,
) -> list[PodInfo]:
    """Filter raw pending Pods to those that could land on this fleet, return as PodInfo.

    Inclusion criteria (all must pass):
    1. Age in [PENDING_POD_MIN_AGE_SECONDS, PENDING_POD_MAX_AGE_SECONDS]. Lower bound is
       already enforced by discovery.build_node_states, but we reapply for safety.
       Upper bound stops stuck zombie pods from inflating min_needed forever.
    2. Constraints match: tolerations + nodeSelector + nodeAffinity must be satisfied by
       at least one node in the group (treated as if our compactor taint were removed —
       since the intent is "could this fleet absorb this pod after we untaint").
    3. Resource sanity: pod's cpu/mem/gpu request must fit on at least one node in the
       group at its allocatable minus DaemonSet overhead (NOT current workload usage).
       DaemonSets run on every node and reserve capacity even when no workload is
       scheduled, so a pod requesting exactly allocatable can never fit. Without this
       filter, an impossibly-large pending pod paralyzes the entire fleet (bin_pack
       returns len(bins), surplus = 0, no taints).

    Pending Pods that pass become PodInfo(is_phantom=False, is_daemonset=False, node_name="")
    suitable for bin_pack_min_nodes.
    """
    if not pending_pods or not group_nodes:
        return []

    now = datetime.now(UTC)
    result: list[PodInfo] = []
    untainted_views = {n.name: node_view_without_taint(n, taint_key) for n in group_nodes}

    for pod in pending_pods:
        meta = getattr(pod, "metadata", None)
        if meta is None:
            continue
        creation_ts = getattr(meta, "creationTimestamp", None)
        if creation_ts is None:
            continue
        age = (now - creation_ts).total_seconds()
        if age < PENDING_POD_MIN_AGE_SECONDS or age > PENDING_POD_MAX_AGE_SECONDS:
            continue

        constraints_ok = False
        for node in group_nodes:
            if _pod_constraints_match(pod, untainted_views[node.name]):
                constraints_ok = True
                break
        if not constraints_ok:
            continue

        cpu_req = pod_cpu_request(pod)
        mem_req = pod_memory_request(pod)
        gpu_req = pod_gpu_request(pod)

        fits_any = False
        for node in group_nodes:
            if cpu_req > node.allocatable_cpu - node.daemonset_cpu:
                continue
            if mem_req > node.allocatable_memory - node.daemonset_memory:
                continue
            if gpu_req > 0 and gpu_req > node.allocatable_gpu - node.daemonset_gpu:
                continue
            fits_any = True
            break
        if not fits_any:
            continue

        result.append(pod_to_podinfo(pod))

    return result
