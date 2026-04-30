"""Per-pool packing analyzer + pod-cost helpers for ``analyze_node_utilization``.

Splits the per-pool printing logic out of the CLI driver so:
  * The runner-pool and workflow-pool sections can share one analyzer.
  * The CLI driver stays small and focused on orchestration.

Public API:
  * ``per_runner_pod_total(runner)`` — pod cost on the runner pool (no GPU).
  * ``per_workflow_pod_total(runner)`` — pod cost on the workflow pool, including
    runner-container-hooks overhead and any GPU request.
  * ``analyze_pool(...)`` — analyze one pool of (NodePoolEntry, RunnerEntry)
    against the provided DaemonSet set + threshold; returns a count of
    below-threshold runners and per-instance slack stats.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from cli_colors import BOLD, CYAN, DIM, GREEN, NC, RED, YELLOW
from instance_specs import INSTANCE_SPECS
from packing import compute_node_slack, find_maximal_combos, find_valid_combos, print_combo

if TYPE_CHECKING:
    from cluster_topology import NodePoolEntry, RunnerEntry
    from daemonset_overhead import DaemonSetOverhead

# Re-imported here so per_workflow_pod_total stays in one module. Kept in sync
# with analyze_node_utilization manually — see docstring there.
HOOKS_OVERHEAD_CPU_M = 320
HOOKS_OVERHEAD_MEM_MI = 522

PodCost = tuple[int, int, int]
PodCostFn = Callable[["RunnerEntry"], PodCost]
AllocFn = Callable[..., dict | None]


def per_runner_pod_total(runner: RunnerEntry) -> PodCost:
    """Resources required by ONE runner pod on the runner pool (no GPU)."""
    return (runner.runner_pod_cpu_m, runner.runner_pod_mem_mi, 0)


def per_workflow_pod_total(runner: RunnerEntry) -> PodCost:
    """Resources required by ONE workflow pod on the workflow pool (GPU if any).

    Adds HOOKS_OVERHEAD_* to account for runner-container-hooks injecting
    helper containers into the workflow pod at runtime.
    """
    return (
        runner.workflow_pod_cpu_m + HOOKS_OVERHEAD_CPU_M,
        runner.workflow_pod_mem_mi + HOOKS_OVERHEAD_MEM_MI,
        runner.workflow_pod_gpu,
    )


def _format_mem(mi: int) -> str:
    if abs(mi) >= 1024:
        return f"{mi / 1024:.1f}Gi"
    return f"{mi}Mi"


def _summarize_pod(runner: RunnerEntry, pod_total_fn: PodCostFn) -> str:
    cpu, mem, gpu = pod_total_fn(runner)
    gpu_note = f", {gpu} GPU" if gpu > 0 else ""
    if pod_total_fn is per_workflow_pod_total:
        return (
            f"{cpu}m CPU, {_format_mem(mem)} RAM{gpu_note} "
            f"(job: {runner.workflow_pod_cpu_m}m+{_format_mem(runner.workflow_pod_mem_mi)},"
            f" hooks: {HOOKS_OVERHEAD_CPU_M}m+{HOOKS_OVERHEAD_MEM_MI}Mi)"
        )
    return f"{cpu}m CPU, {_format_mem(mem)} RAM{gpu_note}"


def _print_alloc_header(instance_type: str, alloc: dict, fleet_name: str) -> None:
    spec = INSTANCE_SPECS[instance_type]
    print(f"\n{'━' * 80}")
    print(f"{BOLD}{CYAN}Node Type: {instance_type} (fleet={fleet_name}){NC}")
    print(
        f"  Total: {spec['vcpu']} vCPU, {spec['memory_gib']}Gi advertised"
        f" ({_format_mem(spec['memory_mi'])} actual)" + (f", {spec['gpu']} GPU" if spec["gpu"] > 0 else "")
    )
    print(f"  Kubelet reserved: {alloc['kube_reserved_cpu_m']}m CPU, {_format_mem(alloc['kube_reserved_mem_mi'])} RAM")
    print(f"  DaemonSet overhead: {alloc['ds_cpu_m']}m CPU, {_format_mem(alloc['ds_mem_mi'])} RAM")
    print(
        f"  {GREEN}Allocatable: "
        f"{alloc['allocatable_cpu_m']}m CPU ({alloc['allocatable_cpu_m'] / 1000:.1f} cores), "
        f"{_format_mem(alloc['allocatable_mem_mi'])} RAM"
        + (f", {alloc['allocatable_gpu']} GPU" if alloc["allocatable_gpu"] > 0 else "")
        + f"{NC}"
    )


def _print_homogeneous(
    runners: list[RunnerEntry],
    alloc: dict,
    threshold: float,
    pod_total_fn: PodCostFn,
) -> int:
    """One line per runner type with single-type packing stats. Returns below-threshold count."""
    below = 0
    print(f"\n  {BOLD}Homogeneous packing (single runner type fills the node):{NC}")
    for r in runners:
        c, m, g = pod_total_fn(r)
        max_by_cpu = alloc["allocatable_cpu_m"] // c if c > 0 else 999
        max_by_mem = alloc["allocatable_mem_mi"] // m if m > 0 else 999
        max_by_gpu = alloc["allocatable_gpu"] // g if g > 0 else 999
        max_pods = min(max_by_cpu, max_by_mem, max_by_gpu)
        used_cpu, used_mem, used_gpu = max_pods * c, max_pods * m, max_pods * g

        cpu_pct = used_cpu / alloc["allocatable_cpu_m"] * 100 if alloc["allocatable_cpu_m"] > 0 else 0
        mem_pct = used_mem / alloc["allocatable_mem_mi"] * 100 if alloc["allocatable_mem_mi"] > 0 else 0
        gpu_pct = used_gpu / alloc["allocatable_gpu"] * 100 if alloc["allocatable_gpu"] > 0 else 100

        if max_by_cpu <= max_by_mem and max_by_cpu <= max_by_gpu:
            bottleneck = "CPU"
        elif max_by_mem <= max_by_gpu:
            bottleneck = "MEM"
        else:
            bottleneck = "GPU"

        worst = min(cpu_pct, mem_pct)
        color = GREEN if worst >= threshold else (YELLOW if worst >= threshold * 0.9 else RED)
        if worst < threshold:
            below += 1
        waste_cpu = alloc["allocatable_cpu_m"] - used_cpu
        waste_mem = alloc["allocatable_mem_mi"] - used_mem

        print(f"    {color}{r.name}{NC}: {max_pods} pods")
        print(
            f"      CPU: {cpu_pct:5.1f}% ({used_cpu}m / {alloc['allocatable_cpu_m']}m) "
            f"waste: {waste_cpu}m ({waste_cpu / 1000:.1f} cores)"
        )
        print(
            f"      MEM: {mem_pct:5.1f}% ({_format_mem(used_mem)} / {_format_mem(alloc['allocatable_mem_mi'])}) "
            f"waste: {_format_mem(waste_mem)}"
        )
        if alloc["allocatable_gpu"] > 0:
            print(f"      GPU: {gpu_pct:5.1f}% ({used_gpu} / {alloc['allocatable_gpu']})")
        print(f"      Bottleneck: {bottleneck}")
    return below


def _print_mixed_combos(
    runners: list[RunnerEntry],
    alloc: dict,
    threshold: float,
    pod_total_fn: PodCostFn,
) -> None:
    """Enumerate maximal mixed combos when there aren't too many runner types."""
    if len(runners) > 8:
        print(f"\n  {DIM}(too many runner types ({len(runners)}) to enumerate mixed combos){NC}")
        return
    print(f"\n  {BOLD}Maximal mixed combos (node fully packed, no room for another pod):{NC}")
    pod_costs = [pod_total_fn(r) for r in runners]
    runner_names = [r.name for r in runners]
    all_combos = find_valid_combos(pod_costs, runner_names, alloc)
    maximal = find_maximal_combos(all_combos, alloc, pod_costs)
    if not maximal:
        print(f"    {DIM}(no valid combos found){NC}")
        return

    maximal.sort(key=lambda c: min(c["cpu_util"], c["mem_util"]), reverse=True)
    best = maximal[:5]
    worst = maximal[-5:] if len(maximal) > 5 else []

    print(f"    Total maximal combos: {len(maximal)}")
    print()
    print(f"    {GREEN}Top {len(best)} most efficient:{NC}")
    for i, combo in enumerate(best):
        print_combo(combo, alloc, threshold, i + 1)
    if worst:
        seen = {tuple(sorted(b["runners"])) for b in best}
        worst = [w for w in worst if tuple(sorted(w["runners"])) not in seen]
        if worst:
            print(f"\n    {RED}Bottom {len(worst)} least efficient (money on the table):{NC}")
            for i, combo in enumerate(worst):
                print_combo(combo, alloc, threshold, i + 1)


def analyze_pool(
    pool_label: str,
    fleet_name: str,
    nodepools: list[NodePoolEntry],
    runners: list[RunnerEntry],
    daemonsets: list[DaemonSetOverhead],
    *,
    threshold: float,
    pod_total_fn: PodCostFn,
    compute_allocatable_fn: AllocFn,
) -> tuple[int, dict[str, dict]]:
    """Analyze packing for one pool. Returns (below_threshold_count, slack_per_instance)."""
    print(f"\n{BOLD}{'═' * 80}{NC}")
    print(f"{BOLD}{pool_label}{NC} (fleet={fleet_name}, runners={len(runners)})")
    print(f"{BOLD}{'═' * 80}{NC}")

    if not runners:
        print(f"  {DIM}(no runners target this pool){NC}")
        return 0, {}
    if not nodepools:
        print(f"  {YELLOW}WARN: no nodepools deployed for fleet '{fleet_name}'{NC}")
        return 0, {}

    by_instance: dict[str, list[NodePoolEntry]] = {}
    for np in nodepools:
        by_instance.setdefault(np.instance_type, []).append(np)
    unknown = [it for it in by_instance if it not in INSTANCE_SPECS]
    if unknown:
        print(f"  {RED}Unknown instance types (add to INSTANCE_SPECS): {unknown}{NC}")

    below_total = 0
    slacks: dict[str, dict] = {}
    for instance_type in sorted(by_instance):
        if instance_type not in INSTANCE_SPECS:
            continue
        alloc = compute_allocatable_fn(instance_type, daemonsets, fleet_name=fleet_name)
        _print_alloc_header(instance_type, alloc, fleet_name)
        print()
        print(f"  {BOLD}Runners targeting this pool:{NC}")
        for r in runners:
            print(f"    - {r.name}: {_summarize_pod(r, pod_total_fn)}")

        below_total += _print_homogeneous(runners, alloc, threshold, pod_total_fn)
        _print_mixed_combos(runners, alloc, threshold, pod_total_fn)

        pod_costs = [pod_total_fn(r) for r in runners]
        slack = compute_node_slack(alloc, pod_costs, homogeneous_only=True)
        if slack:
            slacks[instance_type] = slack
    return below_total, slacks
