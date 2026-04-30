"""Combo enumeration + slack helpers for runner-to-node packing.

Operates on plain (cpu_m, mem_mi, gpu) tuples so the same code can analyze
runner-pool packings (runner pods only) and workflow-pool packings (workflow
pods + GPU + hooks overhead) without caring about which pod type it is.

Consumed by:
  * analyze_node_utilization.py — per-pool packing analysis & summaries.
  * tests — unit tests for the combo math.
"""

from __future__ import annotations

from collections import Counter
from itertools import combinations_with_replacement

from cli_colors import GREEN, NC, RED, YELLOW

PodCost = tuple[int, int, int]


def _format_mem(mi: int) -> str:
    """Local copy to avoid an import cycle with analyze_node_utilization."""
    if abs(mi) >= 1024:
        return f"{mi / 1024:.1f}Gi"
    return f"{mi}Mi"


def find_valid_combos(
    pod_costs: list[PodCost],
    pod_names: list[str],
    alloc: dict,
    max_pods: int = 20,
) -> list[dict]:
    """All combinations of ``pod_costs`` (with replacement) that fit on a node.

    ``pod_costs[i]`` and ``pod_names[i]`` describe the same logical pod. Returns
    one dict per fitting combo with utilization + waste metrics.
    """
    if len(pod_costs) != len(pod_names):
        raise ValueError("pod_costs and pod_names must be the same length")

    combos: list[dict] = []
    avail_cpu = alloc["allocatable_cpu_m"]
    avail_mem = alloc["allocatable_mem_mi"]
    avail_gpu = alloc["allocatable_gpu"]

    # Cap combo size: a 96 vCPU node with 2 vCPU pods = 48 max, but waste analysis
    # cares about the largest combos so we cap to a sensible upper bound.
    max_count = min(max_pods, max(1, avail_cpu // 1000))

    for count in range(1, max_count + 1):
        for combo in combinations_with_replacement(range(len(pod_costs)), count):
            total_cpu = total_mem = total_gpu = 0
            for idx in combo:
                c, m, g = pod_costs[idx]
                total_cpu += c
                total_mem += m
                total_gpu += g
            if total_cpu > avail_cpu or total_mem > avail_mem or total_gpu > avail_gpu:
                continue

            cpu_util = total_cpu / avail_cpu * 100 if avail_cpu > 0 else 0
            mem_util = total_mem / avail_mem * 100 if avail_mem > 0 else 0
            gpu_util = total_gpu / avail_gpu * 100 if avail_gpu > 0 else 0
            combos.append(
                {
                    "runners": [pod_names[i] for i in combo],
                    "count": count,
                    "cpu_used_m": total_cpu,
                    "mem_used_mi": total_mem,
                    "gpu_used": total_gpu,
                    "cpu_util": cpu_util,
                    "mem_util": mem_util,
                    "gpu_util": gpu_util,
                    "cpu_waste_m": avail_cpu - total_cpu,
                    "mem_waste_mi": avail_mem - total_mem,
                    "gpu_waste": avail_gpu - total_gpu,
                }
            )
    return combos


def find_maximal_combos(combos: list[dict], alloc: dict, pod_costs: list[PodCost]) -> list[dict]:
    """Filter to combos where no additional pod (from ``pod_costs``) can still fit."""
    avail_cpu = alloc["allocatable_cpu_m"]
    avail_mem = alloc["allocatable_mem_mi"]
    avail_gpu = alloc["allocatable_gpu"]

    maximal: list[dict] = []
    for combo in combos:
        can_add_more = False
        for c, m, g in pod_costs:
            if (
                combo["cpu_used_m"] + c <= avail_cpu
                and combo["mem_used_mi"] + m <= avail_mem
                and combo["gpu_used"] + g <= avail_gpu
            ):
                can_add_more = True
                break
        if not can_add_more:
            maximal.append(combo)
    return maximal


def compute_node_slack(
    alloc: dict,
    pod_costs: list[PodCost],
    homogeneous_only: bool = False,
) -> dict | None:
    """Min/max unused CPU + MEM across maximal combos.

    When ``homogeneous_only`` is True (or pod_costs has >8 entries), only
    considers single-pod-type packings. Otherwise enumerates all mixed combos.
    Returns None if no valid combos exist.
    """
    if homogeneous_only or len(pod_costs) > 8:
        slacks: list[dict] = []
        for c, m, g in pod_costs:
            max_by_cpu = alloc["allocatable_cpu_m"] // c if c > 0 else 999
            max_by_mem = alloc["allocatable_mem_mi"] // m if m > 0 else 999
            max_by_gpu = alloc["allocatable_gpu"] // g if g > 0 else 999
            max_pods = min(max_by_cpu, max_by_mem, max_by_gpu)
            if max_pods == 0:
                continue
            slacks.append(
                {
                    "cpu_m": alloc["allocatable_cpu_m"] - max_pods * c,
                    "mem_mi": alloc["allocatable_mem_mi"] - max_pods * m,
                }
            )
        if not slacks:
            return None
        return {
            "min_cpu_m": min(s["cpu_m"] for s in slacks),
            "max_cpu_m": max(s["cpu_m"] for s in slacks),
            "min_mem_mi": min(s["mem_mi"] for s in slacks),
            "max_mem_mi": max(s["mem_mi"] for s in slacks),
        }

    pod_names = [f"p{i}" for i in range(len(pod_costs))]
    all_combos = find_valid_combos(pod_costs, pod_names, alloc)
    maximal = find_maximal_combos(all_combos, alloc, pod_costs)
    if not maximal:
        return None
    return {
        "min_cpu_m": min(c["cpu_waste_m"] for c in maximal),
        "max_cpu_m": max(c["cpu_waste_m"] for c in maximal),
        "min_mem_mi": min(c["mem_waste_mi"] for c in maximal),
        "max_mem_mi": max(c["mem_waste_mi"] for c in maximal),
    }


def print_combo(combo: dict, alloc: dict, threshold: float, rank: int) -> None:
    """Print one combo's utilization + waste."""
    counts = Counter(combo["runners"])
    desc = ", ".join(f"{n}x{name}" for name, n in sorted(counts.items()))
    min_util = min(combo["cpu_util"], combo["mem_util"])
    color = GREEN if min_util >= threshold else (YELLOW if min_util >= threshold * 0.9 else RED)
    print(f"      {color}#{rank}{NC} [{desc}]")
    print(
        f"         CPU: {combo['cpu_util']:5.1f}%  "
        f"MEM: {combo['mem_util']:5.1f}%"
        + (f"  GPU: {combo['gpu_util']:5.1f}%" if alloc["allocatable_gpu"] > 0 else "")
        + f"  waste: {combo['cpu_waste_m'] / 1000:.1f}c + {_format_mem(combo['mem_waste_mi'])}"
    )
