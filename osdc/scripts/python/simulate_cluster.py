"""Monte Carlo cluster simulation library for PyTorch CI load.

Places runners one-at-a-time using best-fit bin-packing (Karpenter-like)
until deployed counts approximate observed peak concurrency.

CLI entry point: simulate_cluster_cli.py
"""

import random
from dataclasses import dataclass, field

from analyze_node_utilization import compute_allocatable, per_runner_total
from daemonset_overhead import DaemonSetOverhead


@dataclass
class SimNode:
    """A simulated cluster node."""

    instance_type: str
    total_cpu_m: int
    total_mem_mi: int
    total_gpu: int
    used_cpu_m: int = 0
    used_mem_mi: int = 0
    used_gpu: int = 0
    runner_count: int = 0

    @property
    def remaining_cpu_m(self) -> int:
        return self.total_cpu_m - self.used_cpu_m

    @property
    def remaining_mem_mi(self) -> int:
        return self.total_mem_mi - self.used_mem_mi

    @property
    def remaining_gpu(self) -> int:
        return self.total_gpu - self.used_gpu

    def fits(self, cpu_m: int, mem_mi: int, gpu: int) -> bool:
        return self.remaining_cpu_m >= cpu_m and self.remaining_mem_mi >= mem_mi and self.remaining_gpu >= gpu


@dataclass
class SimResult:
    """Result of a simulation run."""

    nodes: list[SimNode] = field(default_factory=list)
    deployed: dict[str, int] = field(default_factory=dict)
    targets: dict[str, int] = field(default_factory=dict)
    skipped_labels: dict[str, str] = field(default_factory=dict)


def build_peak_targets(
    old_to_new: dict[str, str],
    peak_concurrent: dict[str, int],
) -> tuple[dict[str, int], dict[str, str]]:
    """Collapse old labels to new, summing peaks. Returns (targets, skipped)."""
    targets: dict[str, int] = {}
    skipped: dict[str, str] = {}
    for old_label, peak in peak_concurrent.items():
        if old_label not in old_to_new:
            skipped[old_label] = "no mapping"
            continue
        new_label = old_to_new[old_label]
        targets[new_label] = targets.get(new_label, 0) + peak
    return targets, skipped


def build_weighted_pool(targets: dict[str, int]) -> list[tuple[str, int]]:
    """Create (runner_name, weight) tuples for weighted random drawing."""
    return [(name, weight) for name, weight in targets.items() if weight > 0]


def best_fit_place(
    nodes: list[SimNode],
    cpu_m: int,
    mem_mi: int,
    gpu: int,
    instance_type: str,
) -> int | None:
    """Find matching node with least remaining capacity that fits the runner."""
    best_idx = None
    best_remaining = float("inf")
    for i, node in enumerate(nodes):
        if node.instance_type != instance_type:
            continue
        if not node.fits(cpu_m, mem_mi, gpu):
            continue
        # Score: remaining resources (lower = tighter fit = preferred)
        remaining = node.remaining_cpu_m + node.remaining_mem_mi
        if node.total_gpu > 0:
            # Scale GPU remaining to be comparable to CPU+mem magnitude
            gpu_scale = (node.total_cpu_m + node.total_mem_mi) / max(node.total_gpu, 1)
            remaining += node.remaining_gpu * gpu_scale
        if remaining < best_remaining:
            best_remaining = remaining
            best_idx = i
    return best_idx


def provision_node(
    instance_type: str,
    daemonsets: list[DaemonSetOverhead],
) -> SimNode | None:
    """Create a new SimNode with allocatable capacity after overhead."""
    alloc = compute_allocatable(instance_type, daemonsets)
    if alloc is None:
        return None
    return SimNode(
        instance_type=instance_type,
        total_cpu_m=alloc["allocatable_cpu_m"],
        total_mem_mi=alloc["allocatable_mem_mi"],
        total_gpu=alloc["allocatable_gpu"],
    )


def weighted_mape(deployed: dict[str, int], targets: dict[str, int]) -> float:
    """Weighted MAPE: sum(|deployed_i - target_i|) / sum(target_i)."""
    total_target = sum(targets.values())
    if total_target == 0:
        return 0.0
    total_error = sum(abs(deployed.get(k, 0) - v) for k, v in targets.items())
    return total_error / total_target


def run_simulation(
    runner_defs: list[dict],
    targets: dict[str, int],
    daemonsets: list[DaemonSetOverhead],
    seed: int = 42,
    threshold: float = 0.15,
) -> SimResult:
    """Run Monte Carlo bin-packing: weighted random draw, best-fit place, stop at MAPE <= threshold."""
    rng = random.Random(seed)  # noqa: S311 — simulation, not crypto
    runner_by_name = {r["name"]: r for r in runner_defs}

    # Filter targets to runners that have defs
    active_targets: dict[str, int] = {}
    skipped: dict[str, str] = {}
    for name, peak in targets.items():
        if name in runner_by_name:
            active_targets[name] = peak
        else:
            skipped[name] = "no runner def"

    if not active_targets:
        result = SimResult()
        result.targets = targets
        result.skipped_labels = skipped
        return result

    pool = build_weighted_pool(active_targets)
    if not pool:
        # All targets have zero weight
        return SimResult(targets=active_targets, skipped_labels=skipped)

    names = [p[0] for p in pool]
    weights = [p[1] for p in pool]
    total_target = sum(active_targets.values())
    max_deploy = int(total_target * 1.05)

    nodes: list[SimNode] = []
    deployed: dict[str, int] = dict.fromkeys(active_targets, 0)
    total_deployed = 0
    failed_types: set[str] = set()  # instance types that can't be provisioned

    while True:
        # Check stop conditions
        if total_deployed > 0:
            mape = weighted_mape(deployed, active_targets)
            if mape <= threshold:
                break
        if total_deployed >= max_deploy:
            break

        # Draw a random runner
        chosen = rng.choices(names, weights=weights, k=1)[0]
        runner = runner_by_name[chosen]

        # Skip runners whose instance type can't be provisioned
        if runner["instance_type"] in failed_types:
            # Remove from pool to avoid infinite draws of unplaceable runners
            idx_in_pool = names.index(chosen)
            names.pop(idx_in_pool)
            weights.pop(idx_in_pool)
            if not names:
                break  # No runners left that can be placed
            continue

        cpu_m, mem_mi, gpu = per_runner_total(runner)

        # Try best-fit placement
        idx = best_fit_place(nodes, cpu_m, mem_mi, gpu, runner["instance_type"])
        if idx is not None:
            nodes[idx].used_cpu_m += cpu_m
            nodes[idx].used_mem_mi += mem_mi
            nodes[idx].used_gpu += gpu
            nodes[idx].runner_count += 1
        else:
            # Provision new node
            node = provision_node(runner["instance_type"], daemonsets)
            if node is None:
                failed_types.add(runner["instance_type"])
                continue
            node.used_cpu_m = cpu_m
            node.used_mem_mi = mem_mi
            node.used_gpu = gpu
            node.runner_count = 1
            nodes.append(node)

        deployed[chosen] = deployed.get(chosen, 0) + 1
        total_deployed += 1

    return SimResult(
        nodes=nodes,
        deployed=deployed,
        targets=active_targets,
        skipped_labels=skipped,
    )


def compute_utilization(result: SimResult) -> dict:
    """Compute cluster-wide utilization percentages."""
    total_cpu = sum(n.total_cpu_m for n in result.nodes)
    used_cpu = sum(n.used_cpu_m for n in result.nodes)
    total_mem = sum(n.total_mem_mi for n in result.nodes)
    used_mem = sum(n.used_mem_mi for n in result.nodes)

    gpu_nodes = [n for n in result.nodes if n.total_gpu > 0]
    total_gpu = sum(n.total_gpu for n in gpu_nodes)
    used_gpu = sum(n.used_gpu for n in gpu_nodes)

    return {
        "cpu_pct": (used_cpu / total_cpu * 100) if total_cpu > 0 else 0.0,
        "mem_pct": (used_mem / total_mem * 100) if total_mem > 0 else 0.0,
        "gpu_pct": (used_gpu / total_gpu * 100) if total_gpu > 0 else 0.0,
        "total_cpu_m": total_cpu,
        "used_cpu_m": used_cpu,
        "total_mem_mi": total_mem,
        "used_mem_mi": used_mem,
        "total_gpu": total_gpu,
        "used_gpu": used_gpu,
        "total_nodes": len(result.nodes),
        "gpu_nodes": len(gpu_nodes),
    }
