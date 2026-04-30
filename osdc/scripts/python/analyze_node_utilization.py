#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6.0"]
# ///
"""Analyze runner-to-node packing efficiency for the split-pool topology.

Post-PROACTIVE_CAPACITY model (see PROACTIVE_CAPACITY.md):
  * Runner pods (~750m / 512Mi each) live on a dedicated ``c7i-runner`` pool.
  * Workflow pods (vcpu + memory + GPU + hooks overhead) live on per-runner-class
    workflow pools (m8g, c7i, g5, p6-b200, etc., plus their ``-release`` variants).

Pod sizes are read from the freshly-generated runner manifests on disk
(the ``just analyze-utilization`` recipe regenerates them first).

Usage:
    uv run scripts/python/analyze_node_utilization.py --cluster <cluster_id> [--threshold 90]
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from cli_colors import BOLD, GREEN, NC, RED, YELLOW
from cluster_topology import ClusterTopology, RunnerEntry, resolve_cluster
from daemonset_overhead import DaemonSetOverhead, discover_daemonsets
from instance_specs import ENI_MAX_PODS, INSTANCE_SPECS
from packing import (
    compute_node_slack,
    find_maximal_combos,
    find_valid_combos,
    print_combo,
)
from utilization_report import (
    analyze_pool,
    per_runner_pod_total,
    per_workflow_pod_total,
)

# ARC container-hooks overhead: extra containers injected into the workflow
# (job) pod by runner-container-hooks. Measured at runtime from Karpenter
# scheduling requests vs the def's vcpu/memory; not present in any YAML and
# not yet a tunable knob.
HOOKS_OVERHEAD_CPU_M = 320
HOOKS_OVERHEAD_MEM_MI = 522


def kubelet_reserved(vcpu: int, memory_gib: int, max_pods: int) -> tuple[int, int]:
    """EKS kubelet reserved resources (milliCPU, MiB).

    Formula from awslabs/amazon-eks-ami nodeadm source:
      CPU: 60m first core, 10m next, 5m next 2, 2.5m/core after.
      Memory: 255Mi + 11Mi * max_pods + ~100Mi eviction threshold.
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
    *,
    is_gpu: bool,
    fleet_name: str | None = None,
) -> tuple[int, int]:
    """Sum CPU + memory of DaemonSets running on a node of (is_gpu, fleet_name).

    Filtering rules:
      - gpu_only DaemonSets are skipped when is_gpu=False.
      - When fleet_name is provided: keep DaemonSets with fleet_selector=None
        OR fleet_selector==fleet_name. DaemonSets pinned to a different fleet
        are excluded.
      - When fleet_name is None: include ALL DaemonSets (legacy semantics for
        callers that don't know their pool yet).
    """
    total_cpu = 0
    total_mem = 0
    for ds in daemonsets:
        if ds.gpu_only and not is_gpu:
            continue
        if fleet_name is not None and ds.fleet_selector is not None and ds.fleet_selector != fleet_name:
            continue
        total_cpu += ds.cpu_millicores
        total_mem += ds.memory_mib
    return total_cpu, total_mem


def parse_memory(value: str) -> int:
    """Kubernetes memory string -> MiB. Plain numbers are bytes."""
    value = str(value)
    if value.endswith("Gi"):
        return int(float(value[:-2]) * 1024)
    if value.endswith("Mi"):
        return int(float(value[:-2]))
    if value.endswith("Ki"):
        return int(float(value[:-2]) / 1024)
    return int(int(value) / (1024 * 1024))


def format_mem(mi: int) -> str:
    """Format MiB as GiB if >= 1024."""
    if abs(mi) >= 1024:
        return f"{mi / 1024:.1f}Gi"
    return f"{mi}Mi"


def compute_allocatable(
    instance_type: str,
    daemonsets: list[DaemonSetOverhead],
    *,
    fleet_name: str | None = None,
) -> dict | None:
    """Allocatable resources for an instance after kubelet + DaemonSet overhead.

    ``fleet_name`` is forwarded to ``compute_daemonset_overhead`` so DaemonSets
    pinned to a different fleet are excluded.  When None, every DaemonSet is
    included (matches the pre-pool-aware behavior).
    """
    if instance_type not in INSTANCE_SPECS:
        return None
    spec = INSTANCE_SPECS[instance_type]
    is_gpu = spec["gpu"] > 0

    max_pods = ENI_MAX_PODS.get(instance_type, spec["vcpu"])
    kube_cpu, kube_mem = kubelet_reserved(spec["vcpu"], spec["memory_gib"], max_pods)
    ds_cpu, ds_mem = compute_daemonset_overhead(daemonsets, is_gpu=is_gpu, fleet_name=fleet_name)

    total_cpu_m = spec["vcpu"] * 1000
    total_mem_mi = spec["memory_mi"]
    return {
        "total_cpu_m": total_cpu_m,
        "total_mem_mi": total_mem_mi,
        "kube_reserved_cpu_m": kube_cpu,
        "kube_reserved_mem_mi": kube_mem,
        "ds_cpu_m": ds_cpu,
        "ds_mem_mi": ds_mem,
        "allocatable_cpu_m": total_cpu_m - kube_cpu - ds_cpu,
        "allocatable_mem_mi": total_mem_mi - kube_mem - ds_mem,
        "allocatable_gpu": spec["gpu"],
        "is_gpu": is_gpu,
    }


# ---------------------------------------------------------------------------
# Section printers — each uses analyze_pool() with its own pod_total_fn
# ---------------------------------------------------------------------------


def print_workflow_pool_section(
    topology: ClusterTopology,
    daemonsets: list[DaemonSetOverhead],
    *,
    threshold: float,
) -> tuple[int, dict[str, dict]]:
    print(f"\n{BOLD}{'━' * 80}{NC}")
    print(f"{BOLD}WORKFLOW POOL PACKING{NC}")
    print(f"{BOLD}{'━' * 80}{NC}")

    schedulable = [r for r in topology.runners if r.schedulable]
    if not schedulable:
        print("  (no schedulable runners)")
        return 0, {}

    below_total = 0
    all_slacks: dict[str, dict] = {}
    for fleet in sorted(topology.workflow_pool_fleets):
        for runner_class in (None, "release"):
            pool_runners = [r for r in schedulable if r.workflow_fleet == fleet and r.runner_class == runner_class]
            if not pool_runners:
                continue
            pool_nodepools = [np for np in topology.nodepools if np.fleet == fleet and np.runner_class == runner_class]
            label_suffix = "/release" if runner_class else ""
            below, slacks = analyze_pool(
                f"Workflow Pool [{fleet}{label_suffix}]",
                fleet,
                pool_nodepools,
                pool_runners,
                daemonsets,
                threshold=threshold,
                pod_total_fn=per_workflow_pod_total,
                compute_allocatable_fn=compute_allocatable,
            )
            below_total += below
            prefix = f"{fleet}{label_suffix}"
            for k, v in slacks.items():
                all_slacks[f"{prefix}/{k}"] = v
    return below_total, all_slacks


def print_runner_pool_section(
    topology: ClusterTopology,
    daemonsets: list[DaemonSetOverhead],
    *,
    threshold: float,
) -> tuple[int, dict[str, dict]]:
    print(f"\n{BOLD}{'━' * 80}{NC}")
    print(f"{BOLD}RUNNER POOL PACKING{NC}")
    print(f"{BOLD}{'━' * 80}{NC}")

    if topology.runner_pool_fleet is None:
        print(f"  {YELLOW}No c7i-runner pool found — runner pods cannot be analyzed{NC}")
        return 0, {}
    schedulable = [r for r in topology.runners if r.schedulable]
    pool_nodepools = [np for np in topology.nodepools if np.fleet == topology.runner_pool_fleet]
    return analyze_pool(
        f"Runner Pool [{topology.runner_pool_fleet}]",
        topology.runner_pool_fleet,
        pool_nodepools,
        schedulable,
        daemonsets,
        threshold=threshold,
        pod_total_fn=per_runner_pod_total,
        compute_allocatable_fn=compute_allocatable,
    )


def print_unschedulable_section(topology: ClusterTopology) -> None:
    print(f"\n{BOLD}{'━' * 80}{NC}")
    print(f"{BOLD}UNSCHEDULABLE RUNNERS{NC}")
    print(f"{BOLD}{'━' * 80}{NC}")
    unsched = [r for r in topology.runners if not r.schedulable]
    if not unsched:
        print(f"  {GREEN}(all runners are schedulable on the deployed pools){NC}")
        return
    for r in sorted(unsched, key=lambda x: x.name):
        print(
            f"  {RED}{r.name}{NC}  {r.instance_type}"
            f"  workflow_fleet={r.workflow_fleet}"
            f"  runner_class={r.runner_class}"
            f"  reason={r.schedulable_reason}"
        )


def _print_summary(
    workflow_below: int,
    runner_below: int,
    threshold: float,
    workflow_slacks: dict[str, dict],
    runner_slacks: dict[str, dict],
) -> None:
    total_below = workflow_below + runner_below
    print(f"\n{'━' * 80}")
    if total_below > 0:
        print(
            f"{RED}{BOLD}Found {total_below} runner type(s) below {threshold}% utilization "
            f"(workflow: {workflow_below}, runner pool: {runner_below}){NC}"
        )
    else:
        print(f"{GREEN}{BOLD}All runner types achieve >= {threshold}% utilization{NC}")

    combined: dict[str, dict] = {}
    for k, v in workflow_slacks.items():
        combined[f"workflow:{k}"] = v
    for k, v in runner_slacks.items():
        combined[f"runner:{k}"] = v
    if not combined:
        return
    print(f"\n{BOLD}Unused resource headroom per node (homogeneous packing only):{NC}\n")
    print(f"  {'Section/Node Type':<40} {'Min CPU':>10} {'Max CPU':>10} {'Min MEM':>10} {'Max MEM':>10}")
    print(f"  {'─' * 84}")
    for key in sorted(combined):
        s = combined[key]
        print(
            f"  {key:<40} {s['min_cpu_m']:>7}m   {s['max_cpu_m']:>7}m  "
            f" {format_mem(s['min_mem_mi']):>8}   {format_mem(s['max_mem_mi']):>8}"
        )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _walk_up_for_osdc() -> Path:
    """Walk up from this file to find the upstream osdc/ root."""
    return Path(__file__).resolve().parent.parent.parent


def _resolve_roots() -> tuple[Path, Path]:
    """Resolve (upstream_root, consumer_root) from env vars or by walking up."""
    upstream = Path(os.environ.get("OSDC_UPSTREAM") or _walk_up_for_osdc()).resolve()
    consumer_env = os.environ.get("OSDC_ROOT")
    if consumer_env and Path(consumer_env).is_dir():
        consumer = Path(consumer_env).resolve()
    else:
        candidate = upstream.parent.parent
        consumer = candidate if (candidate / "clusters.yaml").exists() else upstream
    return upstream, consumer.resolve()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyze runner-to-node packing efficiency (split-pool topology)")
    parser.add_argument("--cluster", required=True, help="Cluster id from clusters.yaml")
    parser.add_argument(
        "--threshold",
        type=float,
        default=90.0,
        help="Utilization threshold %% below which combos are flagged (default: 90)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    upstream_root, consumer_root = _resolve_roots()

    daemonsets = discover_daemonsets(
        upstream_root,
        consumer_root=consumer_root if consumer_root != upstream_root else None,
    )
    topology = resolve_cluster(args.cluster, upstream_root=upstream_root, consumer_root=consumer_root)

    print(f"{BOLD}Node Utilization Analysis — cluster '{args.cluster}'{NC}")
    print(f"{'━' * 80}")
    print(f"Region: {topology.region}")
    print(f"Modules: {', '.join(topology.modules)}")
    print(f"Workflow fleets: {', '.join(sorted(topology.workflow_pool_fleets)) or '(none)'}")
    print(f"Runner pool: {topology.runner_pool_fleet or '(none)'}")
    print(f"Runners: {len(topology.runners)} ({sum(1 for r in topology.runners if r.schedulable)} schedulable)")
    print(f"DaemonSets: {len(daemonsets)}")
    print(f"Threshold: {args.threshold}%")

    workflow_below, workflow_slacks = print_workflow_pool_section(topology, daemonsets, threshold=args.threshold)
    runner_below, runner_slacks = print_runner_pool_section(topology, daemonsets, threshold=args.threshold)
    print_unschedulable_section(topology)
    _print_summary(workflow_below, runner_below, args.threshold, workflow_slacks, runner_slacks)
    return 0


# Re-export combo helpers + RunnerEntry so the simulator and tests have a
# stable import surface.
__all__ = [
    "HOOKS_OVERHEAD_CPU_M",
    "HOOKS_OVERHEAD_MEM_MI",
    "RunnerEntry",
    "compute_allocatable",
    "compute_daemonset_overhead",
    "compute_node_slack",
    "find_maximal_combos",
    "find_valid_combos",
    "format_mem",
    "kubelet_reserved",
    "main",
    "parse_args",
    "parse_memory",
    "per_runner_pod_total",
    "per_workflow_pod_total",
    "print_combo",
    "print_runner_pool_section",
    "print_unschedulable_section",
    "print_workflow_pool_section",
]


if __name__ == "__main__":
    sys.exit(main())
