#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6.0"]
# ///
"""CLI for the split-pool Monte Carlo cluster simulation.

Required args:
    --cluster <id>       — must match a top-level entry in clusters.yaml.

Resolution of repo paths is via env vars set by the just recipe:
    OSDC_UPSTREAM        — path to the upstream osdc/ tree (default: walk up).
    OSDC_ROOT            — path to the consumer osdc/ tree (default: upstream).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from cli_colors import BOLD, CYAN, DIM, GREEN, NC, RED, YELLOW
from cluster_topology import ClusterTopology, resolve_cluster
from daemonset_overhead import discover_daemonsets
from simulate_cluster import (
    PoolUtilization,
    SimResult,
    SimulationUtilization,
    build_peak_targets,
    compute_utilization,
    run_simulation,
    weighted_mape,
)

# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


def _walk_up_for_osdc() -> Path:
    """Find the nearest ancestor containing ``clusters.yaml`` (the osdc root)."""
    here = Path(__file__).resolve()
    for parent in [here.parent, *here.parents]:
        if (parent / "clusters.yaml").exists():
            return parent
    # Fall back to two-up (scripts/python/ → osdc/) which matches the repo layout.
    return here.parent.parent.parent


def _resolve_roots() -> tuple[Path, Path]:
    """Resolve (upstream_root, consumer_root) from env vars or filesystem walk."""
    upstream = Path(os.environ.get("OSDC_UPSTREAM") or _walk_up_for_osdc()).resolve()
    consumer = Path(os.environ.get("OSDC_ROOT") or upstream).resolve()
    return upstream, consumer


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def _format_pool(util: PoolUtilization, label: str) -> None:
    print(f"\n{BOLD}{label}{NC}")
    if util.nodes == 0:
        print(f"  {DIM}(no nodes provisioned){NC}")
        return
    cpu_color = GREEN if util.cpu_pct >= 80 else YELLOW
    mem_color = GREEN if util.mem_pct >= 80 else YELLOW
    print(
        f"  {cpu_color}vCPU:   {util.cpu_pct:5.1f}%{NC}  "
        f"({util.used_cpu_m / 1000:.0f} / {util.total_cpu_m / 1000:.0f} cores)"
    )
    print(
        f"  {mem_color}Memory: {util.mem_pct:5.1f}%{NC}  "
        f"({util.used_mem_mi / 1024:.0f} / {util.total_mem_mi / 1024:.0f} GiB)"
    )
    if util.total_gpu > 0:
        gpu_color = GREEN if util.gpu_pct >= 80 else YELLOW
        print(f"  {gpu_color}GPU:    {util.gpu_pct:5.1f}%{NC}  ({util.used_gpu} / {util.total_gpu} GPUs)")
    print(f"  Nodes: {util.nodes}")


def _print_node_breakdown(result: SimResult) -> None:
    """Group nodes by ``(fleet, instance_type, runner_class)`` and print counts."""
    by_key: dict[tuple[str, str, str | None], list] = {}
    for node in (*result.workflow_nodes, *result.runner_nodes):
        key = (node.fleet, node.instance_type, node.runner_class)
        by_key.setdefault(key, []).append(node)

    if not by_key:
        return
    print(f"\n{BOLD}Nodes by fleet / instance / runner-class:{NC}")
    print(f"  {'Fleet':<14} {'Instance':<22} {'Class':<10} {'Nodes':>5}  {'CPU%':>6}  {'Mem%':>6}  {'GPU%':>6}")
    print(f"  {'─' * 80}")
    for key in sorted(by_key):
        ns = by_key[key]
        nodes = len(ns)
        cpu_pct = sum(n.used_cpu_m for n in ns) / max(sum(n.cpu_m for n in ns), 1) * 100
        mem_pct = sum(n.used_mem_mi for n in ns) / max(sum(n.mem_mi for n in ns), 1) * 100
        total_gpu = sum(n.gpu for n in ns)
        gpu_pct = sum(n.used_gpu for n in ns) / total_gpu * 100 if total_gpu > 0 else 0.0
        rc = key[2] or "-"
        gpu_str = f"{gpu_pct:5.1f}%" if total_gpu > 0 else "  -   "
        print(f"  {key[0]:<14} {key[1]:<22} {rc:<10} {nodes:>5}  {cpu_pct:5.1f}%  {mem_pct:5.1f}%  {gpu_str}")


def _print_deployment_accuracy(result: SimResult) -> None:
    total_deployed = sum(result.deployed.values())
    total_target = sum(result.targets.values())
    mape = weighted_mape(result.deployed, result.targets)
    print(f"\n{BOLD}Deployment accuracy:{NC}\n")
    print(f"  Total deployed: {total_deployed} / {total_target} target")
    print(f"  Weighted MAPE: {mape:.1%}")
    if not result.targets:
        return
    print(f"\n  {'Runner':<35} {'Deployed':>9} {'Target':>8} {'Diff':>8}")
    print(f"  {'─' * 64}")
    for name in sorted(result.targets):
        dep = result.deployed.get(name, 0)
        tgt = result.targets[name]
        diff = dep - tgt
        # Within 15% of target (or within 1 absolute) counts as green.
        color = GREEN if abs(diff) <= max(1, tgt * 0.15) else YELLOW
        print(f"  {color}{name:<35} {dep:>9} {tgt:>8} {diff:>+8}{NC}")


def _print_results(result: SimResult, util: SimulationUtilization) -> None:
    print(f"\n{BOLD}{'━' * 80}{NC}")
    print(f"{BOLD}{CYAN}Cluster Simulation Results{NC}")
    print(f"{'━' * 80}")
    if result.skipped:
        print(f"\n{YELLOW}Skipped runner targets ({len(result.skipped)}):{NC}")
        for name in result.skipped:
            print(f"  {DIM}{name}{NC}")
    _format_pool(util.workflow, "Workflow Pool")
    _format_pool(util.runner, "Runner Pool")
    _print_combined_summary(util)
    _print_node_breakdown(result)
    _print_deployment_accuracy(result)
    print(f"\n{'━' * 80}")


def _print_combined_summary(util: SimulationUtilization) -> None:
    """One-line cluster-wide totals across both pools."""
    total_nodes = util.workflow.nodes + util.runner.nodes
    total_cpu_m = util.workflow.total_cpu_m + util.runner.total_cpu_m
    used_cpu_m = util.workflow.used_cpu_m + util.runner.used_cpu_m
    total_mem_mi = util.workflow.total_mem_mi + util.runner.total_mem_mi
    used_mem_mi = util.workflow.used_mem_mi + util.runner.used_mem_mi
    cpu_pct = (used_cpu_m / total_cpu_m * 100) if total_cpu_m > 0 else 0.0
    mem_pct = (used_mem_mi / total_mem_mi * 100) if total_mem_mi > 0 else 0.0
    print(f"\n{BOLD}Cluster-wide totals (both pools):{NC}")
    print(
        f"  {total_nodes} nodes  |  "
        f"vCPU {cpu_pct:.1f}% ({used_cpu_m / 1000:.0f}/{total_cpu_m / 1000:.0f}c)  |  "
        f"Mem {mem_pct:.1f}% ({used_mem_mi / 1024:.0f}/{total_mem_mi / 1024:.0f} GiB)"
    )


# ---------------------------------------------------------------------------
# Multi-round
# ---------------------------------------------------------------------------


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = max(0, min(int(len(s) * pct / 100), len(s) - 1))
    return s[idx]


def _run_multi(
    topology: ClusterTopology,
    targets: dict[str, int],
    daemonsets,
    *,
    seed: int,
    threshold: float,
    rounds: int,
) -> list[SimulationUtilization]:
    out: list[SimulationUtilization] = []
    for i in range(rounds):
        result = run_simulation(topology, targets, daemonsets, seed=seed + i, threshold=threshold)
        out.append(compute_utilization(result))
    return out


def _summarize(values: list[float]) -> tuple[float, float, float, float]:
    """Return (avg, p50, p25, worst) — worst = lowest utilization."""
    if not values:
        return 0.0, 0.0, 0.0, 0.0
    avg = sum(values) / len(values)
    return avg, _percentile(values, 50), _percentile(values, 25), min(values)


def _print_pool_multi(label: str, utils: list[PoolUtilization]) -> None:
    print(f"\n{BOLD}{label}{NC}")
    if not any(u.nodes for u in utils):
        print(f"  {DIM}(no nodes provisioned across rounds){NC}")
        return
    print(f"  {'Resource':<10} {'Avg':>8} {'p50':>8} {'p25':>8} {'Worst':>8}")
    print(f"  {'─' * 42}")
    for label_key, getter in (
        ("vCPU", lambda u: u.cpu_pct),
        ("Memory", lambda u: u.mem_pct),
    ):
        avg, p50, p25, worst = _summarize([getter(u) for u in utils])
        color = GREEN if avg >= 80 else YELLOW
        print(f"  {color}{label_key:<10} {avg:>7.1f}% {p50:>7.1f}% {p25:>7.1f}% {worst:>7.1f}%{NC}")
    if any(u.total_gpu > 0 for u in utils):
        avg, p50, p25, worst = _summarize([u.gpu_pct for u in utils])
        color = GREEN if avg >= 80 else YELLOW
        print(f"  {color}{'GPU':<10} {avg:>7.1f}% {p50:>7.1f}% {p25:>7.1f}% {worst:>7.1f}%{NC}")
    node_counts = [u.nodes for u in utils]
    print(f"  Nodes: avg {sum(node_counts) / len(node_counts):.1f}, min {min(node_counts)}, max {max(node_counts)}")


def _print_multi(results: list[SimulationUtilization], *, seed: int, rounds: int) -> None:
    print(f"\n{BOLD}{'━' * 80}{NC}")
    print(f"{BOLD}{CYAN}Multi-Round Summary ({rounds} rounds, seeds {seed}..{seed + rounds - 1}){NC}")
    print(f"{'━' * 80}")
    _print_pool_multi("Workflow Pool", [u.workflow for u in results])
    _print_pool_multi("Runner Pool", [u.runner for u in results])
    print(f"\n{'━' * 80}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Monte Carlo cluster simulation for PyTorch CI load")
    parser.add_argument("--cluster", required=True, help="Cluster id from clusters.yaml")
    parser.add_argument("--seed", type=int, default=42, help="Random seed (default: 42)")
    parser.add_argument("--threshold", type=float, default=0.15, help="Weighted MAPE threshold to stop (default: 0.15)")
    parser.add_argument(
        "--rounds", type=int, default=1, help="Run N rounds with different seeds and print summary stats"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    upstream_root, consumer_root = _resolve_roots()

    # PEAK_CONCURRENT is a global pytorch/pytorch snapshot — the staging
    # cluster sees a tiny fraction of that load. Warn so users don't read
    # the staging numbers as a real capacity assessment.
    if "staging" in args.cluster:
        print(
            f"{YELLOW}WARNING: PEAK_CONCURRENT is a global pytorch/pytorch snapshot — "
            f"staging numbers will be inflated.{NC}"
        )

    daemonsets = discover_daemonsets(
        upstream_root,
        consumer_root=consumer_root if consumer_root != upstream_root else None,
    )
    topology = resolve_cluster(args.cluster, upstream_root=upstream_root, consumer_root=consumer_root)

    targets = build_peak_targets(topology.runners)
    if not targets:
        print(f"{RED}No schedulable runner targets matched the workload snapshot for cluster '{args.cluster}'{NC}")
        return 1

    print(f"{BOLD}Monte Carlo Cluster Simulation — {args.cluster}{NC}")
    print(f"{'━' * 80}")
    print(
        f"Region: {topology.region or '(unknown)'}  |  "
        f"Modules: {len(topology.modules)}  |  "
        f"Runner pool: {topology.runner_pool_fleet or '(none)'}  |  "
        f"Workflow pools: {len(topology.workflow_pool_fleets)}"
    )
    print(
        f"Seed: {args.seed}  |  MAPE threshold: {args.threshold:.0%}  |  "
        f"Targets: {len(targets)}  |  DaemonSets: {len(daemonsets)}"
    )

    if args.rounds > 1:
        results = _run_multi(
            topology, targets, daemonsets, seed=args.seed, threshold=args.threshold, rounds=args.rounds
        )
        _print_multi(results, seed=args.seed, rounds=args.rounds)
    else:
        result = run_simulation(topology, targets, daemonsets, seed=args.seed, threshold=args.threshold)
        util = compute_utilization(result)
        _print_results(result, util)
    return 0


if __name__ == "__main__":
    sys.exit(main())
