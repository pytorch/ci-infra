#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6.0"]
# ///
"""Analyze runner-to-node packing efficiency.

Groups runners by instance type, computes all valid combinations that fit
on a node (after subtracting kubelet + DaemonSet + runner sidecar overhead),
and reports suboptimal configurations where resources are wasted.

Usage:
    uv run scripts/python/analyze_node_utilization.py [--threshold 90] [--show-daemonsets]

Reads:
    - modules/arc-runners/defs/*.yaml (+ consumer overrides)
    - modules/nodepools/defs/*.yaml (+ consumer overrides)
"""

import argparse
import os
import sys
from itertools import combinations_with_replacement
from pathlib import Path

import yaml
from cli_colors import BOLD, CYAN, DIM, GREEN, NC, RED, YELLOW
from daemonset_overhead import DaemonSetOverhead, discover_daemonsets

# ---------------------------------------------------------------------------
# AWS instance type specs: vCPU, memory_gib, gpu_count
# From AWS documentation — add entries when supporting new instance types.
# ---------------------------------------------------------------------------
INSTANCE_SPECS = {
    # x86 CPU — compute-optimized (~2 GiB/core)
    "c7i.12xlarge": {"vcpu": 48, "memory_gib": 96, "gpu": 0},
    "c7i.metal-24xl": {"vcpu": 96, "memory_gib": 192, "gpu": 0},
    "c7a.48xlarge": {"vcpu": 192, "memory_gib": 384, "gpu": 0},
    # x86 CPU — balanced (~4 GiB/core)
    "m6i.32xlarge": {"vcpu": 128, "memory_gib": 512, "gpu": 0},
    "m7i.48xlarge": {"vcpu": 192, "memory_gib": 768, "gpu": 0},
    # x86 CPU — memory-optimized (~8 GiB/core)
    "r5.24xlarge": {"vcpu": 96, "memory_gib": 768, "gpu": 0},
    "r7i.48xlarge": {"vcpu": 192, "memory_gib": 1536, "gpu": 0},
    "r7a.48xlarge": {"vcpu": 192, "memory_gib": 1536, "gpu": 0},
    # ARM64 CPU
    "m8g.48xlarge": {"vcpu": 192, "memory_gib": 768, "gpu": 0},
    "r7g.16xlarge": {"vcpu": 64, "memory_gib": 512, "gpu": 0},
    # GPU instances — 1-GPU
    "g4dn.8xlarge": {"vcpu": 32, "memory_gib": 128, "gpu": 1},
    "g5.8xlarge": {"vcpu": 32, "memory_gib": 128, "gpu": 1},
    "g6.8xlarge": {"vcpu": 32, "memory_gib": 128, "gpu": 1},
    # GPU instances — 4-GPU
    "g4dn.12xlarge": {"vcpu": 48, "memory_gib": 192, "gpu": 4},
    "g5.12xlarge": {"vcpu": 48, "memory_gib": 192, "gpu": 4},
    "g6.12xlarge": {"vcpu": 48, "memory_gib": 192, "gpu": 4},
    # GPU instances — 8-GPU
    "g4dn.metal": {"vcpu": 96, "memory_gib": 384, "gpu": 8},
    "g5.48xlarge": {"vcpu": 192, "memory_gib": 768, "gpu": 8},
    "g6.48xlarge": {"vcpu": 192, "memory_gib": 768, "gpu": 8},
    "p6-b200.48xlarge": {"vcpu": 192, "memory_gib": 2048, "gpu": 8},
    # BuildKit instances (used by modules/buildkit/)
    "m8gd.24xlarge": {"vcpu": 96, "memory_gib": 384, "gpu": 0},
    "m6id.24xlarge": {"vcpu": 96, "memory_gib": 384, "gpu": 0},
    "c7gd.16xlarge": {"vcpu": 64, "memory_gib": 128, "gpu": 0},
    "m7gd.16xlarge": {"vcpu": 64, "memory_gib": 256, "gpu": 0},
    "m8gd.16xlarge": {"vcpu": 64, "memory_gib": 256, "gpu": 0},
}

# ---------------------------------------------------------------------------
# EKS max pods per instance type (from ENI limits).
# Source: awslabs/amazon-eks-ami eni-max-pods.txt
# The kubelet memory reservation formula uses max_pods, NOT vCPU count.
# ---------------------------------------------------------------------------
ENI_MAX_PODS = {
    # Runner node instance types
    "c7i.12xlarge": 234,
    "c7i.metal-24xl": 737,
    "c7a.48xlarge": 737,
    "m6i.32xlarge": 737,
    "m7i.48xlarge": 737,
    "r5.24xlarge": 737,
    "r7i.48xlarge": 737,
    "r7a.48xlarge": 737,
    "m8g.48xlarge": 737,
    "r7g.16xlarge": 737,
    "g4dn.8xlarge": 58,
    "g5.8xlarge": 234,
    "g6.8xlarge": 234,
    "g4dn.12xlarge": 234,
    "g5.12xlarge": 737,
    "g6.12xlarge": 234,
    "g4dn.metal": 737,
    "g5.48xlarge": 345,
    "g6.48xlarge": 737,
    "p6-b200.48xlarge": 198,
    # BuildKit instance types
    "m8gd.24xlarge": 737,
    "m6id.24xlarge": 737,
    "c7gd.16xlarge": 737,
    "m7gd.16xlarge": 737,
    "m8gd.16xlarge": 737,
}


# Runner pod sidecar (the ARC orchestrator container, not the $job container)
RUNNER_SIDECAR_CPU_M = 750
RUNNER_SIDECAR_MEM_MI = 512


def kubelet_reserved(vcpu: int, memory_gib: int, max_pods: int) -> tuple[int, int]:
    """Estimate EKS kubelet reserved resources (milliCPU, MiB).

    EKS formula (from awslabs/amazon-eks-ami nodeadm source):
    - CPU: 60m first core, 10m next, 5m next 2, 2.5m/core after
    - Memory: 255Mi + 11Mi * max_pods + ~100Mi eviction threshold
      (max_pods is derived from ENI limits, NOT vCPU count)
    """
    if vcpu <= 1:
        reserved_cpu = 60
    elif vcpu <= 2:
        reserved_cpu = 70
    elif vcpu <= 4:
        reserved_cpu = 80
    else:
        reserved_cpu = 80 + int((vcpu - 4) * 2.5)

    reserved_mem = 255 + 11 * max_pods + 100
    return reserved_cpu, reserved_mem


def compute_daemonset_overhead(
    daemonsets: list[DaemonSetOverhead],
    is_gpu: bool,
) -> tuple[int, int]:
    """Total DaemonSet overhead on a runner node (milliCPU, MiB)."""
    total_cpu = 0
    total_mem = 0
    for ds in daemonsets:
        if ds.gpu_only and not is_gpu:
            continue
        total_cpu += ds.cpu_millicores
        total_mem += ds.memory_mib
    return total_cpu, total_mem


def parse_memory(value: str) -> int:
    """Parse Kubernetes memory string to MiB."""
    value = str(value)
    if value.endswith("Gi"):
        return int(float(value[:-2]) * 1024)
    if value.endswith("Mi"):
        return int(float(value[:-2]))
    if value.endswith("Ki"):
        return int(float(value[:-2]) / 1024)
    # Plain number = bytes
    return int(int(value) / (1024 * 1024))


def load_runner_defs(dirs: list[Path]) -> list[dict]:
    """Load all runner definitions from the given directories."""
    runners = []
    for d in dirs:
        if not d.exists():
            continue
        for f in sorted(d.glob("*.yaml")):
            with open(f) as fh:
                data = yaml.safe_load(fh)
            if not data or "runner" not in data:
                continue
            r = data["runner"]
            runners.append(
                {
                    "name": r["name"],
                    "instance_type": r["instance_type"],
                    "vcpu": int(r["vcpu"]),
                    "memory_mi": parse_memory(r["memory"]),
                    "gpu": int(r.get("gpu", 0)),
                    "file": f.name,
                }
            )
    return runners


def load_nodepool_defs(dirs: list[Path]) -> dict:
    """Load nodepool defs and return a dict of instance_type -> def."""
    nodepools = {}
    for d in dirs:
        if not d.exists():
            continue
        for f in sorted(d.glob("*.yaml")):
            with open(f) as fh:
                data = yaml.safe_load(fh)
            if not data or "nodepool" not in data:
                continue
            np = data["nodepool"]
            nodepools[np["instance_type"]] = {
                "name": np["name"],
                "gpu": np.get("gpu", False),
            }
    return nodepools


def compute_allocatable(
    instance_type: str,
    daemonsets: list[DaemonSetOverhead],
) -> dict:
    """Compute allocatable resources for an instance type after overhead."""
    if instance_type not in INSTANCE_SPECS:
        return None
    spec = INSTANCE_SPECS[instance_type]
    is_gpu = spec["gpu"] > 0

    max_pods = ENI_MAX_PODS.get(instance_type, spec["vcpu"])  # fallback to vcpu if unknown
    kube_cpu, kube_mem = kubelet_reserved(spec["vcpu"], spec["memory_gib"], max_pods)
    ds_cpu, ds_mem = compute_daemonset_overhead(daemonsets, is_gpu)

    total_cpu_m = spec["vcpu"] * 1000
    total_mem_mi = spec["memory_gib"] * 1024

    alloc_cpu_m = total_cpu_m - kube_cpu - ds_cpu
    alloc_mem_mi = total_mem_mi - kube_mem - ds_mem

    return {
        "total_cpu_m": total_cpu_m,
        "total_mem_mi": total_mem_mi,
        "kube_reserved_cpu_m": kube_cpu,
        "kube_reserved_mem_mi": kube_mem,
        "ds_cpu_m": ds_cpu,
        "ds_mem_mi": ds_mem,
        "allocatable_cpu_m": alloc_cpu_m,
        "allocatable_mem_mi": alloc_mem_mi,
        "allocatable_gpu": spec["gpu"],
        "is_gpu": is_gpu,
    }


def per_runner_total(runner: dict) -> tuple[int, int, int]:
    """Total resources per runner pod (job container + sidecar)."""
    cpu = runner["vcpu"] * 1000 + RUNNER_SIDECAR_CPU_M
    mem = runner["memory_mi"] + RUNNER_SIDECAR_MEM_MI
    gpu = runner["gpu"]
    return cpu, mem, gpu


def find_valid_combos(
    runners: list[dict],
    alloc: dict,
    max_pods: int = 20,
) -> list[dict]:
    """Find all valid combinations of runner pods that fit on the node.

    Uses combinations_with_replacement to find multi-pod packings.
    Returns list of combo dicts with utilization info.
    """
    combos = []
    avail_cpu = alloc["allocatable_cpu_m"]
    avail_mem = alloc["allocatable_mem_mi"]
    avail_gpu = alloc["allocatable_gpu"]

    # Cap the max number of pods we'll consider per combo
    # (a node with 96 vCPU and 2-vCPU runners = 48 pods max, but in practice
    # the largest combos are what matter for waste analysis)
    max_count = min(max_pods, max(1, avail_cpu // 1000))

    for count in range(1, max_count + 1):
        for combo in combinations_with_replacement(range(len(runners)), count):
            total_cpu = 0
            total_mem = 0
            total_gpu = 0
            for idx in combo:
                c, m, g = per_runner_total(runners[idx])
                total_cpu += c
                total_mem += m
                total_gpu += g

            # Check if combo fits
            if total_cpu > avail_cpu:
                continue
            if total_mem > avail_mem:
                continue
            if total_gpu > avail_gpu:
                continue

            cpu_util = total_cpu / avail_cpu * 100 if avail_cpu > 0 else 0
            mem_util = total_mem / avail_mem * 100 if avail_mem > 0 else 0
            gpu_util = total_gpu / avail_gpu * 100 if avail_gpu > 0 else 0

            runner_names = [runners[i]["name"] for i in combo]
            combos.append(
                {
                    "runners": runner_names,
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


def find_maximal_combos(combos: list[dict], alloc: dict, runners: list[dict]) -> list[dict]:
    """Filter to maximal combos: those where no additional runner can fit.

    These are the realistic packing scenarios — a node is full when you
    can't add any more pods.
    """
    avail_cpu = alloc["allocatable_cpu_m"]
    avail_mem = alloc["allocatable_mem_mi"]
    avail_gpu = alloc["allocatable_gpu"]

    maximal = []
    for combo in combos:
        # Check if any runner could still fit
        can_add_more = False
        for r in runners:
            c, m, g = per_runner_total(r)
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


def format_mem(mi: int) -> str:
    """Format MiB as GiB if >= 1024."""
    if abs(mi) >= 1024:
        return f"{mi / 1024:.1f}Gi"
    return f"{mi}Mi"


def compute_node_slack(
    alloc: dict,
    runners: list[dict],
    homogeneous_only: bool = False,
) -> dict | None:
    """Compute min/max unused CPU and memory across maximal combos.

    When homogeneous_only is True (or >8 runner types), only considers
    single-runner-type packings. Otherwise enumerates all mixed combos.

    Returns dict with min_cpu_m, max_cpu_m, min_mem_mi, max_mem_mi,
    or None if no valid combos exist.
    """
    if homogeneous_only or len(runners) > 8:
        slacks = []
        for r in runners:
            c, m, g = per_runner_total(r)
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

    all_combos = find_valid_combos(runners, alloc)
    maximal = find_maximal_combos(all_combos, alloc, runners)
    if not maximal:
        return None

    return {
        "min_cpu_m": min(c["cpu_waste_m"] for c in maximal),
        "max_cpu_m": max(c["cpu_waste_m"] for c in maximal),
        "min_mem_mi": min(c["mem_waste_mi"] for c in maximal),
        "max_mem_mi": max(c["mem_waste_mi"] for c in maximal),
    }


def print_node_analysis(
    instance_type: str,
    alloc: dict,
    runners: list[dict],
    threshold: float,
):
    """Analyze and print utilization for one node type."""
    print(f"\n{'━' * 80}")
    print(f"{BOLD}{CYAN}Node Type: {instance_type}{NC}")
    spec = INSTANCE_SPECS[instance_type]
    print(
        f"  Total: {spec['vcpu']} vCPU, {spec['memory_gib']}Gi RAM"
        + (f", {spec['gpu']} GPU" if spec["gpu"] > 0 else "")
    )
    print(f"  Kubelet reserved: {alloc['kube_reserved_cpu_m']}m CPU, {format_mem(alloc['kube_reserved_mem_mi'])} RAM")
    print(f"  DaemonSet overhead: {alloc['ds_cpu_m']}m CPU, {format_mem(alloc['ds_mem_mi'])} RAM")
    print(
        f"  {GREEN}Allocatable for runners: "
        f"{alloc['allocatable_cpu_m']}m CPU ({alloc['allocatable_cpu_m'] / 1000:.1f} cores), "
        f"{format_mem(alloc['allocatable_mem_mi'])} RAM"
        + (f", {alloc['allocatable_gpu']} GPU" if alloc["allocatable_gpu"] > 0 else "")
        + f"{NC}"
    )
    print()

    # List runners targeting this node
    print(f"  {BOLD}Runners targeting this node:{NC}")
    for r in runners:
        c, m, g = per_runner_total(r)
        sidecar_note = f" (job: {r['vcpu']}c+{format_mem(r['memory_mi'])}, sidecar: 750m+512Mi)"
        gpu_note = f", {r['gpu']} GPU" if r["gpu"] > 0 else ""
        print(f"    - {r['name']}: {c}m CPU, {format_mem(m)} RAM{gpu_note}{sidecar_note}")

    # Homogeneous packing: how many of each runner type fits alone?
    print(f"\n  {BOLD}Homogeneous packing (single runner type fills the node):{NC}")
    for r in runners:
        c, m, g = per_runner_total(r)
        max_by_cpu = alloc["allocatable_cpu_m"] // c if c > 0 else 999
        max_by_mem = alloc["allocatable_mem_mi"] // m if m > 0 else 999
        max_by_gpu = alloc["allocatable_gpu"] // g if g > 0 else 999
        max_pods = min(max_by_cpu, max_by_mem, max_by_gpu)

        used_cpu = max_pods * c
        used_mem = max_pods * m
        used_gpu = max_pods * g
        cpu_pct = used_cpu / alloc["allocatable_cpu_m"] * 100
        mem_pct = used_mem / alloc["allocatable_mem_mi"] * 100
        gpu_pct = used_gpu / alloc["allocatable_gpu"] * 100 if alloc["allocatable_gpu"] > 0 else 100

        bottleneck = (
            "CPU"
            if max_by_cpu <= max_by_mem and max_by_cpu <= max_by_gpu
            else ("MEM" if max_by_mem <= max_by_gpu else "GPU")
        )
        waste_cpu = alloc["allocatable_cpu_m"] - used_cpu
        waste_mem = alloc["allocatable_mem_mi"] - used_mem

        color = (
            GREEN
            if min(cpu_pct, mem_pct) >= threshold
            else (YELLOW if min(cpu_pct, mem_pct) >= threshold * 0.9 else RED)
        )

        print(f"    {color}{r['name']}{NC}: {max_pods} pods")
        print(
            f"      CPU: {cpu_pct:5.1f}% ({used_cpu}m / {alloc['allocatable_cpu_m']}m) "
            f"waste: {waste_cpu}m ({waste_cpu / 1000:.1f} cores)"
        )
        print(
            f"      MEM: {mem_pct:5.1f}% ({format_mem(used_mem)} / {format_mem(alloc['allocatable_mem_mi'])}) "
            f"waste: {format_mem(waste_mem)}"
        )
        if alloc["allocatable_gpu"] > 0:
            print(f"      GPU: {gpu_pct:5.1f}% ({used_gpu} / {alloc['allocatable_gpu']})")
        print(f"      Bottleneck: {bottleneck}")

    # Find maximal mixed combos (only if few enough runners to enumerate)
    if len(runners) <= 8:
        print(f"\n  {BOLD}Maximal mixed combos (node fully packed, no room for another pod):{NC}")
        all_combos = find_valid_combos(runners, alloc)
        maximal = find_maximal_combos(all_combos, alloc, runners)

        if not maximal:
            print(f"    {DIM}(no valid combos found){NC}")
            return

        # Sort by worst utilization (min of CPU%, MEM%) descending
        maximal.sort(key=lambda c: min(c["cpu_util"], c["mem_util"]), reverse=True)

        # Show top 5 best and worst 5
        best = maximal[:5]
        worst = maximal[-5:] if len(maximal) > 5 else []

        print(f"    Total maximal combos: {len(maximal)}")
        print()
        print(f"    {GREEN}Top {len(best)} most efficient:{NC}")
        for i, combo in enumerate(best):
            _print_combo(combo, alloc, threshold, i + 1)

        if worst:
            seen = {tuple(sorted(b["runners"])) for b in best}
            worst = [w for w in worst if tuple(sorted(w["runners"])) not in seen]
            if worst:
                print(f"\n    {RED}Bottom {len(worst)} least efficient (money on the table):{NC}")
                for i, combo in enumerate(worst):
                    _print_combo(combo, alloc, threshold, i + 1)
    else:
        print(f"\n  {DIM}(too many runner types ({len(runners)}) to enumerate mixed combos){NC}")


def _print_combo(combo: dict, alloc: dict, threshold: float, rank: int):
    """Print a single combo's utilization."""
    from collections import Counter

    counts = Counter(combo["runners"])
    desc = ", ".join(f"{n}x{name}" for name, n in sorted(counts.items()))
    min_util = min(combo["cpu_util"], combo["mem_util"])
    color = GREEN if min_util >= threshold else (YELLOW if min_util >= threshold * 0.9 else RED)
    print(f"      {color}#{rank}{NC} [{desc}]")
    print(
        f"         CPU: {combo['cpu_util']:5.1f}%  "
        f"MEM: {combo['mem_util']:5.1f}%"
        + (f"  GPU: {combo['gpu_util']:5.1f}%" if alloc["allocatable_gpu"] > 0 else "")
        + f"  waste: {combo['cpu_waste_m'] / 1000:.1f}c + {format_mem(combo['mem_waste_mi'])}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Analyze runner-to-node packing efficiency",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=90.0,
        help="Utilization threshold %% below which combos are flagged (default: 90)",
    )
    parser.add_argument(
        "--show-daemonsets",
        action="store_true",
        help="Print discovered DaemonSets and their resource overhead, then exit",
    )
    args = parser.parse_args(argv)

    # Resolve directories
    script_dir = Path(__file__).resolve().parent
    upstream_dir = script_dir.parent.parent  # upstream/osdc
    # Check if we're in a consumer repo
    consumer_root = Path(os.environ.get("OSDC_ROOT", ""))
    if not consumer_root.is_dir():
        # Try to find consumer root by walking up
        candidate = upstream_dir.parent.parent  # consumer osdc/
        consumer_root = candidate if (candidate / "clusters.yaml").exists() else upstream_dir

    # Discover DaemonSet overhead dynamically from manifests
    daemonsets = discover_daemonsets(
        upstream_dir,
        consumer_root=consumer_root if consumer_root != upstream_dir else None,
    )

    if args.show_daemonsets:
        print(f"{BOLD}Discovered DaemonSets ({len(daemonsets)}):{NC}\n")
        for ds in daemonsets:
            gpu_tag = f" {YELLOW}[GPU-only]{NC}" if ds.gpu_only else ""
            print(f"  {ds.name}: {ds.cpu_millicores}m CPU, {ds.memory_mib}Mi RAM{gpu_tag}")
            print(f"    {DIM}source: {ds.source}{NC}")
        return 0

    # Collect runner defs from upstream + consumer
    runner_dirs = [
        upstream_dir / "modules" / "arc-runners" / "defs",
    ]
    # Add consumer runner def dirs
    if consumer_root != upstream_dir:
        consumer_modules = consumer_root / "modules"
        if consumer_modules.exists():
            for mod_dir in sorted(consumer_modules.iterdir()):
                defs = mod_dir / "defs"
                if defs.exists() and any(defs.glob("*.yaml")):
                    # Check if any def has runner key
                    for f in defs.glob("*.yaml"):
                        with open(f) as fh:
                            d = yaml.safe_load(fh)
                        if d and "runner" in d:
                            runner_dirs.append(defs)
                            break

    # Collect nodepool defs
    nodepool_dirs = [
        upstream_dir / "modules" / "nodepools" / "defs",
    ]
    if consumer_root != upstream_dir:
        consumer_modules = consumer_root / "modules"
        if consumer_modules.exists():
            for mod_dir in sorted(consumer_modules.iterdir()):
                defs = mod_dir / "defs"
                if defs.exists() and any(defs.glob("*.yaml")):
                    for f in defs.glob("*.yaml"):
                        with open(f) as fh:
                            d = yaml.safe_load(fh)
                        if d and "nodepool" in d:
                            nodepool_dirs.append(defs)
                            break

    print(f"{BOLD}Node Utilization Analysis{NC}")
    print(f"{'━' * 80}")
    print(f"Runner def dirs: {', '.join(str(d) for d in runner_dirs)}")
    print(f"NodePool def dirs: {', '.join(str(d) for d in nodepool_dirs)}")
    print(f"Utilization threshold: {args.threshold}%")

    runners = load_runner_defs(runner_dirs)
    _nodepools = load_nodepool_defs(nodepool_dirs)  # loaded for future use

    if not runners:
        print(f"{RED}No runner definitions found{NC}")
        return 1

    # Group runners by instance type
    by_instance: dict[str, list[dict]] = {}
    for r in runners:
        by_instance.setdefault(r["instance_type"], []).append(r)

    # Warn about unknown instance types
    unknown = [it for it in by_instance if it not in INSTANCE_SPECS]
    if unknown:
        print(f"\n{RED}Unknown instance types (add to INSTANCE_SPECS): {unknown}{NC}")

    # Analyze each instance type and collect slack data
    total_issues = 0
    node_slacks: dict[str, dict] = {}
    for instance_type in sorted(by_instance.keys()):
        if instance_type not in INSTANCE_SPECS:
            continue
        alloc = compute_allocatable(instance_type, daemonsets)
        type_runners = by_instance[instance_type]
        print_node_analysis(instance_type, alloc, type_runners, args.threshold)

        # Compute slack for summary (homogeneous only — excludes mixed combos)
        slack = compute_node_slack(alloc, type_runners, homogeneous_only=True)
        if slack:
            node_slacks[instance_type] = slack

        # Count suboptimal homogeneous packings
        for r in type_runners:
            c, m, g = per_runner_total(r)
            max_by_cpu = alloc["allocatable_cpu_m"] // c if c > 0 else 999
            max_by_mem = alloc["allocatable_mem_mi"] // m if m > 0 else 999
            max_by_gpu = alloc["allocatable_gpu"] // g if g > 0 else 999
            max_pods = min(max_by_cpu, max_by_mem, max_by_gpu)
            used_cpu = max_pods * c
            used_mem = max_pods * m
            cpu_pct = used_cpu / alloc["allocatable_cpu_m"] * 100
            mem_pct = used_mem / alloc["allocatable_mem_mi"] * 100
            if min(cpu_pct, mem_pct) < args.threshold:
                total_issues += 1

    print(f"\n{'━' * 80}")
    if total_issues > 0:
        print(
            f"{RED}{BOLD}Found {total_issues} runner type(s) with homogeneous utilization below {args.threshold}%{NC}"
        )
    else:
        print(f"{GREEN}{BOLD}All runner types achieve >= {args.threshold}% utilization in homogeneous packing{NC}")

    # --- Slack / headroom summary ---
    if node_slacks:
        print(f"\n{'━' * 80}")
        print(f"{BOLD}Unused resource headroom per node (homogeneous packing only):{NC}\n")
        print(f"  {'Node Type':<22} {'Min CPU':>10} {'Max CPU':>10} {'Min MEM':>10} {'Max MEM':>10}")
        print(f"  {'─' * 64}")

        global_min_cpu = None
        global_max_cpu = None
        global_min_mem = None
        global_max_mem = None

        for inst in sorted(node_slacks.keys()):
            s = node_slacks[inst]
            print(
                f"  {inst:<22} {s['min_cpu_m']:>7}m   {s['max_cpu_m']:>7}m  "
                f" {format_mem(s['min_mem_mi']):>8}   {format_mem(s['max_mem_mi']):>8}"
            )
            if global_min_cpu is None or s["min_cpu_m"] < global_min_cpu:
                global_min_cpu = s["min_cpu_m"]
            if global_max_cpu is None or s["max_cpu_m"] > global_max_cpu:
                global_max_cpu = s["max_cpu_m"]
            if global_min_mem is None or s["min_mem_mi"] < global_min_mem:
                global_min_mem = s["min_mem_mi"]
            if global_max_mem is None or s["max_mem_mi"] > global_max_mem:
                global_max_mem = s["max_mem_mi"]

        print(f"  {'─' * 64}")
        print(
            f"  {BOLD}{'WORST CASE':<22}{NC} {global_min_cpu:>7}m   {global_max_cpu:>7}m  "
            f" {format_mem(global_min_mem):>8}   {format_mem(global_max_mem):>8}"
        )
        print()
        print(
            f"  The tightest node has only {BOLD}{global_min_cpu}m CPU{NC}"
            f" and {BOLD}{format_mem(global_min_mem)} RAM{NC} free."
        )
        print("  Any new DaemonSet must fit within these limits or runners will fail to schedule.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
