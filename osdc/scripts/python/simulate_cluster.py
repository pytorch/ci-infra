"""Monte Carlo cluster simulation library for PyTorch CI load (split-pool model).

Each runner deployment now occupies TWO pods on TWO different node pools (per
PROACTIVE_CAPACITY.md):

  1. A workflow pod on the per-runner-class workflow pool (heavy: vCPU + memory
     + GPU + container-hooks overhead).
  2. A runner pod on the dedicated ``c7i-runner`` runner pool (light: ~750m
     CPU + ~512Mi memory).

The simulator runs both placements per runner draw and reports utilization for
both pools separately. Workload data is sourced from ``pytorch_workload_data``
(global pytorch/pytorch snapshot). Cluster topology is supplied by
``cluster_topology.resolve_cluster``.

Placement / provisioning helpers live in ``sim_placement`` to keep this file
under the 400-line ceiling. They are re-exported from this module so callers
have a single entry point.

CLI entry point: ``simulate_cluster_cli.py``.
"""

from __future__ import annotations

import random
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from sim_placement import (
    best_fit_place_runner,
    best_fit_place_workflow,
    per_runner_pod_total,
    per_workflow_pod_total,
    provision_runner_node,
    provision_workflow_node,
)

if TYPE_CHECKING:
    from cluster_topology import ClusterTopology, NodePoolEntry, RunnerEntry
    from daemonset_overhead import DaemonSetOverhead


# Re-export placement helpers so simulate_cluster remains the public entry
# point — callers import from here, not from sim_placement directly.
__all__ = [
    "PoolUtilization",
    "SimNode",
    "SimResult",
    "SimulationUtilization",
    "best_fit_place_runner",
    "best_fit_place_workflow",
    "build_peak_targets",
    "build_weighted_pool",
    "compute_utilization",
    "per_runner_pod_total",
    "per_workflow_pod_total",
    "provision_runner_node",
    "provision_workflow_node",
    "run_simulation",
    "weighted_mape",
]


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class SimNode:
    """A simulated cluster node.

    ``fleet`` is the ``node-fleet`` label (e.g. ``c7i-runner``, ``g4dn``)
    and disambiguates workflow vs runner pool nodes. ``runner_class`` is
    set only for release-tagged workflow nodes; runner-pool nodes always
    have ``runner_class=None``.
    """

    instance_type: str
    fleet: str
    runner_class: str | None
    cpu_m: int
    mem_mi: int
    gpu: int
    used_cpu_m: int = 0
    used_mem_mi: int = 0
    used_gpu: int = 0
    pod_count: int = 0

    @property
    def remaining_cpu_m(self) -> int:
        return self.cpu_m - self.used_cpu_m

    @property
    def remaining_mem_mi(self) -> int:
        return self.mem_mi - self.used_mem_mi

    @property
    def remaining_gpu(self) -> int:
        return self.gpu - self.used_gpu

    def can_fit(self, cpu_m: int, mem_mi: int, gpu: int) -> bool:
        return self.remaining_cpu_m >= cpu_m and self.remaining_mem_mi >= mem_mi and self.remaining_gpu >= gpu

    def allocate(self, cpu_m: int, mem_mi: int, gpu: int) -> None:
        self.used_cpu_m += cpu_m
        self.used_mem_mi += mem_mi
        self.used_gpu += gpu
        self.pod_count += 1


@dataclass
class PoolUtilization:
    """Per-pool resource accounting (workflow OR runner pool)."""

    nodes: int = 0
    used_cpu_m: int = 0
    total_cpu_m: int = 0
    used_mem_mi: int = 0
    total_mem_mi: int = 0
    used_gpu: int = 0
    total_gpu: int = 0

    @property
    def cpu_pct(self) -> float:
        return (self.used_cpu_m / self.total_cpu_m * 100) if self.total_cpu_m > 0 else 0.0

    @property
    def mem_pct(self) -> float:
        return (self.used_mem_mi / self.total_mem_mi * 100) if self.total_mem_mi > 0 else 0.0

    @property
    def gpu_pct(self) -> float:
        return (self.used_gpu / self.total_gpu * 100) if self.total_gpu > 0 else 0.0


@dataclass
class SimulationUtilization:
    """Utilization broken out by pool."""

    workflow: PoolUtilization
    runner: PoolUtilization


@dataclass
class SimResult:
    """Result of a simulation run.

    ``workflow_nodes`` are nodes from the per-runner-class workflow pools;
    ``runner_nodes`` are nodes from the dedicated runner pool. ``skipped``
    lists target runner names that were not in topology or were not
    schedulable.
    """

    workflow_nodes: list[SimNode] = field(default_factory=list)
    runner_nodes: list[SimNode] = field(default_factory=list)
    deployed: dict[str, int] = field(default_factory=dict)
    targets: dict[str, int] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Target / pool builders
# ---------------------------------------------------------------------------


def build_peak_targets(runners: list[RunnerEntry]) -> dict[str, int]:
    """Map ``RunnerEntry`` → peak concurrent count via ``OLD_TO_NEW_LABEL``.

    Sums peaks for any old labels that map to the same new runner name.
    Only includes runners that are schedulable in the topology.
    """
    # Local import keeps the workload snapshot a runtime concern — keeps
    # test fixtures cheap and avoids a top-of-file dependency cycle.
    from pytorch_workload_data import OLD_TO_NEW_LABEL, PEAK_CONCURRENT

    schedulable_names = {r.name for r in runners if r.schedulable}
    targets: dict[str, int] = defaultdict(int)
    for old_label, peak in PEAK_CONCURRENT.items():
        new_name = OLD_TO_NEW_LABEL.get(old_label)
        if new_name is None or new_name not in schedulable_names:
            continue
        targets[new_name] += peak
    return dict(targets)


def build_weighted_pool(targets: dict[str, int]) -> list[str]:
    """Expand ``{name: weight}`` into a list with each name repeated ``weight`` times."""
    pool: list[str] = []
    for name, weight in targets.items():
        if weight <= 0:
            continue
        pool.extend([name] * weight)
    return pool


def weighted_mape(deployed: dict[str, int], targets: dict[str, int]) -> float:
    """Weighted MAPE: ``sum(|deployed - target|) / sum(target)``."""
    total_target = sum(targets.values())
    if total_target == 0:
        return 0.0
    total_error = sum(abs(deployed.get(k, 0) - v) for k, v in targets.items())
    return total_error / total_target


# ---------------------------------------------------------------------------
# Simulation loop
# ---------------------------------------------------------------------------


def _stop_condition(deployed: dict[str, int], targets: dict[str, int], threshold: float, max_deploy: int) -> bool:
    """Stop when MAPE drops to threshold or we hit the safety cap."""
    total = sum(deployed.values())
    if total == 0:
        return False
    if total >= max_deploy:
        return True
    return weighted_mape(deployed, targets) <= threshold


def _place_runner_draw(
    runner: RunnerEntry,
    workflow_nodes: list[SimNode],
    runner_nodes: list[SimNode],
    runner_pool_fleet: str | None,
    topology_nodepools: list[NodePoolEntry],
    daemonsets: list[DaemonSetOverhead],
) -> bool:
    """Place one runner draw (workflow pod, then runner pod). Returns False when either provisioning fails."""
    wf_node = best_fit_place_workflow(runner, workflow_nodes)
    if wf_node is None:
        wf_node = provision_workflow_node(runner, daemonsets)
        if wf_node is None:
            return False
        workflow_nodes.append(wf_node)
    wf_node.allocate(*per_workflow_pod_total(runner))

    if runner_pool_fleet is None:
        # No dedicated runner pool — runner pod placement is skipped, but
        # the workflow placement above counts as a deployment.
        return True
    r_cpu, r_mem, _ = per_runner_pod_total(runner)
    rn_node = best_fit_place_runner(runner_pool_fleet, r_cpu, r_mem, runner_nodes)
    if rn_node is None:
        rn_node = provision_runner_node(runner_pool_fleet, topology_nodepools, daemonsets)
        if rn_node is None:
            return False
        runner_nodes.append(rn_node)
    rn_node.allocate(r_cpu, r_mem, 0)
    return True


def run_simulation(
    topology: ClusterTopology,
    targets: dict[str, int],
    daemonsets: list[DaemonSetOverhead],
    *,
    seed: int = 42,
    threshold: float = 0.15,
) -> SimResult:
    """Run the two-phase simulation: workflow pod, then runner pod."""
    schedulable = {r.name: r for r in topology.runners if r.schedulable}
    active_targets = {name: count for name, count in targets.items() if name in schedulable}
    skipped = sorted(name for name in targets if name not in schedulable)

    if not active_targets:
        return SimResult(targets=active_targets, skipped=skipped)

    weighted_pool = build_weighted_pool(active_targets)
    if not weighted_pool:
        return SimResult(targets=active_targets, skipped=skipped)

    workflow_nodes: list[SimNode] = []
    runner_nodes: list[SimNode] = []
    deployed: dict[str, int] = dict.fromkeys(active_targets, 0)
    failed: set[str] = set()
    max_deploy = max(int(sum(active_targets.values()) * 1.05), 1)
    rng = random.Random(seed)  # noqa: S311 — simulation, not crypto.

    while not _stop_condition(deployed, active_targets, threshold, max_deploy):
        eligible = [name for name in weighted_pool if name not in failed]
        if not eligible:
            break
        runner_name = rng.choice(eligible)
        runner = schedulable[runner_name]
        ok = _place_runner_draw(
            runner, workflow_nodes, runner_nodes, topology.runner_pool_fleet, topology.nodepools, daemonsets
        )
        if not ok:
            failed.add(runner_name)
            continue
        deployed[runner_name] += 1

    return SimResult(
        workflow_nodes=workflow_nodes,
        runner_nodes=runner_nodes,
        deployed=deployed,
        targets=active_targets,
        skipped=skipped,
    )


# ---------------------------------------------------------------------------
# Utilization
# ---------------------------------------------------------------------------


def _aggregate(nodes: list[SimNode]) -> PoolUtilization:
    util = PoolUtilization()
    for node in nodes:
        util.nodes += 1
        util.used_cpu_m += node.used_cpu_m
        util.total_cpu_m += node.cpu_m
        util.used_mem_mi += node.used_mem_mi
        util.total_mem_mi += node.mem_mi
        util.used_gpu += node.used_gpu
        util.total_gpu += node.gpu
    return util


def compute_utilization(result: SimResult) -> SimulationUtilization:
    """Aggregate per-pool utilization from a SimResult."""
    return SimulationUtilization(
        workflow=_aggregate(result.workflow_nodes),
        runner=_aggregate(result.runner_nodes),
    )
