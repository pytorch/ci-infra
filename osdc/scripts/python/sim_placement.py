"""Best-fit placement and node-provisioning helpers for the split-pool simulator.

Extracted from ``simulate_cluster.py`` to keep that module under the 400-line
ceiling. Operates on the ``SimNode`` dataclass defined there. All placement
matches on ``(fleet, runner_class)`` for workflow nodes and ``fleet`` only
for runner-pool nodes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from analyze_node_utilization import HOOKS_OVERHEAD_CPU_M, HOOKS_OVERHEAD_MEM_MI, compute_allocatable
from instance_specs import INSTANCE_SPECS

if TYPE_CHECKING:
    from cluster_topology import NodePoolEntry, RunnerEntry
    from daemonset_overhead import DaemonSetOverhead
    from simulate_cluster import SimNode


# ---------------------------------------------------------------------------
# Pod cost helpers — per-pod resource totals
# ---------------------------------------------------------------------------


def per_runner_pod_total(runner: RunnerEntry) -> tuple[int, int, int]:
    """Runner pod cost (cpu_m, mem_mi, gpu) — sidecar only, no hooks overhead.

    The runner pod runs on the runner pool and never carries the workflow
    container or its hooks. GPU is always 0.
    """
    return runner.runner_pod_cpu_m, runner.runner_pod_mem_mi, 0


def per_workflow_pod_total(runner: RunnerEntry) -> tuple[int, int, int]:
    """Workflow pod cost (cpu_m, mem_mi, gpu) — job container + hooks overhead."""
    cpu = runner.workflow_pod_cpu_m + HOOKS_OVERHEAD_CPU_M
    mem = runner.workflow_pod_mem_mi + HOOKS_OVERHEAD_MEM_MI
    return cpu, mem, runner.workflow_pod_gpu


def _allocatable_for(
    instance_type: str,
    daemonsets: list[DaemonSetOverhead],
    fleet_name: str,
) -> tuple[int, int] | None:
    """Wrap ``compute_allocatable`` to return ``(cpu_m, mem_mi)`` or None."""
    alloc = compute_allocatable(instance_type, daemonsets, fleet_name=fleet_name)
    if alloc is None:
        return None
    return alloc["allocatable_cpu_m"], alloc["allocatable_mem_mi"]


# ---------------------------------------------------------------------------
# Best-fit placement
# ---------------------------------------------------------------------------


def _best_fit_node(candidates: list[SimNode], cpu_m: int, mem_mi: int, gpu: int) -> SimNode | None:
    """Pick the candidate with the tightest remaining capacity that fits.

    Score = remaining CPU + remaining memory + (GPU-scaled remaining GPU).
    Lower score wins. Returns None when nothing fits.
    """
    best: SimNode | None = None
    best_score: float = float("inf")
    for node in candidates:
        if not node.can_fit(cpu_m, mem_mi, gpu):
            continue
        score: float = node.remaining_cpu_m + node.remaining_mem_mi
        if node.gpu > 0:
            # Scale GPU remaining to be comparable to CPU+mem magnitude — a
            # raw GPU count of 1-8 would be drowned out by 100k CPU / mem.
            gpu_scale = (node.cpu_m + node.mem_mi) / max(node.gpu, 1)
            score += node.remaining_gpu * gpu_scale
        if score < best_score:
            best_score = score
            best = node
    return best


def best_fit_place_workflow(runner: RunnerEntry, workflow_nodes: list[SimNode]) -> SimNode | None:
    """Place one workflow pod on the best-fit existing workflow node.

    Matches on ``(fleet, runner_class)`` so release runners only land on
    release nodes and CPU runners never land on GPU nodes of the wrong fleet.
    """
    cpu, mem, gpu = per_workflow_pod_total(runner)
    candidates = [
        n for n in workflow_nodes if n.fleet == runner.workflow_fleet and n.runner_class == runner.runner_class
    ]
    return _best_fit_node(candidates, cpu, mem, gpu)


def best_fit_place_runner(
    runner_pool_fleet: str,
    runner_pod_cpu_m: int,
    runner_pod_mem_mi: int,
    runner_nodes: list[SimNode],
) -> SimNode | None:
    """Place one runner pod on the best-fit existing runner-pool node."""
    candidates = [n for n in runner_nodes if n.fleet == runner_pool_fleet]
    return _best_fit_node(candidates, runner_pod_cpu_m, runner_pod_mem_mi, 0)


# ---------------------------------------------------------------------------
# Provisioning
# ---------------------------------------------------------------------------


def provision_workflow_node(runner: RunnerEntry, daemonsets: list[DaemonSetOverhead]):
    """Create a workflow-pool node sized for ``runner.instance_type``.

    Returns None when the instance is not in INSTANCE_SPECS so the caller
    can mark the runner as failed and stop drawing it.
    """
    # Local import avoids a cycle: simulate_cluster imports from sim_placement.
    from simulate_cluster import SimNode

    instance_type = runner.instance_type
    alloc = _allocatable_for(instance_type, daemonsets, fleet_name=runner.workflow_fleet)
    if alloc is None:
        return None
    cpu_m, mem_mi = alloc
    gpu = INSTANCE_SPECS[instance_type]["gpu"]
    return SimNode(
        instance_type=instance_type,
        fleet=runner.workflow_fleet,
        runner_class=runner.runner_class,
        cpu_m=cpu_m,
        mem_mi=mem_mi,
        gpu=gpu,
    )


def provision_runner_node(
    runner_pool_fleet: str,
    nodepools: list[NodePoolEntry],
    daemonsets: list[DaemonSetOverhead],
):
    """Create a runner-pool node using the largest instance from that fleet.

    Karpenter's weighted scoring prefers larger instances first when the
    pool has demand, so picking the largest matches observed behavior.
    Returns None when the fleet has no nodepools or the chosen instance
    is unknown.
    """
    from simulate_cluster import SimNode

    candidates = [np for np in nodepools if np.fleet == runner_pool_fleet]
    if not candidates:
        return None
    chosen = max(candidates, key=lambda np: INSTANCE_SPECS.get(np.instance_type, {"vcpu": 0})["vcpu"])
    alloc = _allocatable_for(chosen.instance_type, daemonsets, fleet_name=runner_pool_fleet)
    if alloc is None:
        return None
    cpu_m, mem_mi = alloc
    return SimNode(
        instance_type=chosen.instance_type,
        fleet=runner_pool_fleet,
        runner_class=None,
        cpu_m=cpu_m,
        mem_mi=mem_mi,
        gpu=0,
    )
