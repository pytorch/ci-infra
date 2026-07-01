"""Bin-pack fit check shared between packing and taint decisions.

Lives in its own module to avoid an import cycle: both ``packing`` and
``taints`` need this helper, and ``packing`` already imports from
``pending`` which imports from ``taints``.
"""

from models import NodeState, PodInfo


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
                "gpu_remaining": node.allocatable_gpu - node.total_gpu_used,
            }
        )

    sorted_pods = sorted(pods, key=lambda p: (p.gpu_request, p.cpu_request), reverse=True)
    for pod in sorted_pods:
        placed = False
        for b in bins:
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
            return False
    return True
