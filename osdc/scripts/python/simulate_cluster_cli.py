#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6.0"]
# ///
"""CLI for Monte Carlo cluster simulation.

Usage:
    uv run scripts/python/simulate_cluster_cli.py [--seed 42] [--threshold 0.15]
"""

import argparse
import os
import sys
from pathlib import Path

from analyze_node_utilization import load_runner_defs
from cli_colors import BOLD, CYAN, DIM, GREEN, NC, RED, YELLOW
from daemonset_overhead import discover_daemonsets
from pytorch_workload_data import OLD_TO_NEW_LABEL, PEAK_CONCURRENT
from simulate_cluster import (
    SimNode,
    SimResult,
    build_peak_targets,
    compute_utilization,
    run_simulation,
    weighted_mape,
)


def print_results(result: SimResult, utilization: dict) -> None:
    """Print simulation results."""
    print(f"\n{BOLD}{'━' * 80}{NC}")
    print(f"{BOLD}{CYAN}Cluster Simulation Results{NC}")
    print(f"{'━' * 80}")

    if result.skipped_labels:
        print(f"\n{YELLOW}Skipped labels ({len(result.skipped_labels)}):{NC}")
        for label, reason in sorted(result.skipped_labels.items()):
            print(f"  {DIM}{label}: {reason}{NC}")

    _print_node_table(result)
    _print_deployment_accuracy(result)
    _print_utilization(utilization)


def _print_node_table(result: SimResult) -> None:
    by_type: dict[str, list[SimNode]] = {}
    for node in result.nodes:
        by_type.setdefault(node.instance_type, []).append(node)

    print(f"\n{BOLD}Nodes by instance type:{NC}\n")
    hdr = f"  {'Instance Type':<22} {'Nodes':>5} {'vCPU Used':>10} {'vCPU Total':>10} {'Mem Used':>10} {'Mem Total':>10} {'GPU':>5}"
    print(hdr)
    print(f"  {'─' * 78}")
    for itype in sorted(by_type):
        ns = by_type[itype]
        uc, tc = sum(n.used_cpu_m for n in ns), sum(n.total_cpu_m for n in ns)
        um, tm = sum(n.used_mem_mi for n in ns), sum(n.total_mem_mi for n in ns)
        ug, tg = sum(n.used_gpu for n in ns), sum(n.total_gpu for n in ns)
        gpu_str = f"{ug}/{tg}" if tg > 0 else "-"
        print(
            f"  {itype:<22} {len(ns):>5} {uc / 1000:>9.1f}c {tc / 1000:>9.1f}c {um / 1024:>8.1f}Gi {tm / 1024:>8.1f}Gi {gpu_str:>5}"
        )


def _print_deployment_accuracy(result: SimResult) -> None:
    total_deployed = sum(result.deployed.values())
    total_target = sum(result.targets.values())
    mape = weighted_mape(result.deployed, result.targets)
    print(f"\n{BOLD}Deployment accuracy:{NC}\n")
    print(f"  Total deployed: {total_deployed} / {total_target} target")
    print(f"  Weighted MAPE: {mape:.1%}")
    print(f"\n  {'Runner':<35} {'Deployed':>8} {'Target':>8} {'Diff':>8}")
    print(f"  {'─' * 63}")
    for name in sorted(result.targets):
        dep, tgt = result.deployed.get(name, 0), result.targets[name]
        diff = dep - tgt
        color = GREEN if abs(diff) <= max(1, tgt * 0.15) else YELLOW
        print(f"  {color}{name:<35} {dep:>8} {tgt:>8} {diff:>+8}{NC}")


def _print_utilization(utilization: dict) -> None:
    print(f"\n{BOLD}Cluster-wide utilization:{NC}\n")
    _RESOURCE_ROWS = [
        ("cpu_pct", "vCPU", "used_cpu_m", "total_cpu_m", 1000, "cores"),
        ("mem_pct", "Memory", "used_mem_mi", "total_mem_mi", 1024, "GiB"),
    ]
    for pct_key, label, used_key, total_key, divisor, unit in _RESOURCE_ROWS:
        color = GREEN if utilization[pct_key] >= 80 else YELLOW
        print(
            f"  {color}{label + ':':8s}{utilization[pct_key]:5.1f}%{NC}  ({utilization[used_key] / divisor:.0f} / {utilization[total_key] / divisor:.0f} {unit})"
        )
    if utilization["total_gpu"] > 0:
        color = GREEN if utilization["gpu_pct"] >= 80 else YELLOW
        print(
            f"  {color}{'GPU:':8s}{utilization['gpu_pct']:5.1f}%{NC}  ({utilization['used_gpu']} / {utilization['total_gpu']} GPUs across {utilization['gpu_nodes']} nodes)"
        )
    print(f"\n  Total nodes: {utilization['total_nodes']}")
    print(f"{'━' * 80}")


def _percentile(values: list[float], pct: float) -> float:
    """Return the pct-th percentile (0-100) using nearest-rank."""
    if not values:
        return 0.0
    s = sorted(values)
    idx = max(0, min(int(len(s) * pct / 100), len(s) - 1))
    return s[idx]


def _run_multi(
    runners: list[dict],
    targets: dict[str, int],
    daemonsets: list,
    skipped_mapping: dict[str, str],
    args,
) -> int:
    """Run multiple rounds with different seeds, print summary statistics."""
    utils: list[dict] = []
    for i in range(args.rounds):
        seed = args.seed + i
        result = run_simulation(runners, targets, daemonsets, seed=seed, threshold=args.threshold)
        utils.append(compute_utilization(result))

    print(f"\n{BOLD}{'━' * 80}{NC}")
    print(f"{BOLD}{CYAN}Multi-Round Summary ({args.rounds} rounds, seeds {args.seed}..{args.seed + args.rounds - 1}){NC}")
    print(f"{'━' * 80}")

    if skipped_mapping:
        print(f"\n{YELLOW}Unmapped old labels: {len(skipped_mapping)}{NC}")

    _print_multi_summary(utils)
    return 0


def _print_multi_summary(utils: list[dict]) -> None:
    """Print percentile summary of cluster-wide utilization across rounds."""
    _ROWS = [("cpu_pct", "vCPU"), ("mem_pct", "Memory")]
    has_gpu = any(u["total_gpu"] > 0 for u in utils)

    # Header
    print(f"\n{BOLD}Cluster-wide utilization:{NC}\n")
    print(f"  {'Resource':<10} {'Avg':>8} {'p50':>8} {'p25':>8} {'Worst':>8}")
    print(f"  {'─' * 42}")

    for pct_key, label in _ROWS:
        values = [u[pct_key] for u in utils]
        avg = sum(values) / len(values)
        p50 = _percentile(values, 50)
        p25 = _percentile(values, 25)
        worst = min(values)  # lowest utilization = most wasteful
        color = GREEN if avg >= 80 else YELLOW
        print(f"  {color}{label:<10} {avg:>7.1f}% {p50:>7.1f}% {p25:>7.1f}% {worst:>7.1f}%{NC}")

    if has_gpu:
        values = [u["gpu_pct"] for u in utils]
        avg = sum(values) / len(values)
        p50 = _percentile(values, 50)
        p25 = _percentile(values, 25)
        worst = min(values)
        color = GREEN if avg >= 80 else YELLOW
        print(f"  {color}{'GPU':<10} {avg:>7.1f}% {p50:>7.1f}% {p25:>7.1f}% {worst:>7.1f}%{NC}")

    # Node count summary
    node_counts = [u["total_nodes"] for u in utils]
    avg_nodes = sum(node_counts) / len(node_counts)
    print(f"\n  Nodes: avg {avg_nodes:.0f}, min {min(node_counts)}, max {max(node_counts)}")
    print(f"{'━' * 80}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Monte Carlo cluster simulation for PyTorch CI load",
    )
    parser.add_argument("--seed", type=int, default=42, help="Random seed (default: 42)")
    parser.add_argument("--threshold", type=float, default=0.15, help="Weighted MAPE threshold to stop (default: 0.15)")
    parser.add_argument("--rounds", type=int, default=1, help="Run N rounds with different seeds, print summary statistics")
    parser.add_argument("--upstream-dir", type=Path, default=None, help="Path to upstream osdc/ directory")
    parser.add_argument("--consumer-root", type=Path, default=None, help="Path to consumer osdc/ directory")
    args = parser.parse_args(argv)

    # Resolve directories
    script_dir = Path(__file__).resolve().parent
    upstream_dir = args.upstream_dir or script_dir.parent.parent
    consumer_root = args.consumer_root
    if consumer_root is None:
        env_root = os.environ.get("OSDC_ROOT", "")
        if env_root and Path(env_root).is_dir():
            consumer_root = Path(env_root)
        else:
            candidate = upstream_dir.parent.parent
            consumer_root = candidate if (candidate / "clusters.yaml").exists() else upstream_dir

    # Discover DaemonSet overhead
    daemonsets = discover_daemonsets(
        upstream_dir,
        consumer_root=consumer_root if consumer_root != upstream_dir else None,
    )

    # Collect runner defs (upstream + consumer modules containing runner YAMLs)
    runner_dirs = [upstream_dir / "modules" / "arc-runners" / "defs"]
    if consumer_root != upstream_dir and (consumer_root / "modules").exists():
        import yaml

        for mod_dir in sorted((consumer_root / "modules").iterdir()):
            defs = mod_dir / "defs"
            if not defs.exists():
                continue
            if any((d := yaml.safe_load(f.read_text())) and "runner" in d for f in defs.glob("*.yaml")):
                runner_dirs.append(defs)

    runners = load_runner_defs(runner_dirs)
    if not runners:
        print(f"{RED}No runner definitions found{NC}")
        return 1

    print(f"{BOLD}Monte Carlo Cluster Simulation{NC}\n{'━' * 80}")
    print(
        f"Seed: {args.seed}  |  MAPE threshold: {args.threshold:.0%}  |  Runners: {len(runners)}  |  DaemonSets: {len(daemonsets)}"
    )

    # Build targets
    targets, skipped_mapping = build_peak_targets(OLD_TO_NEW_LABEL, PEAK_CONCURRENT)
    print(f"Peak target runner types: {len(targets)} (mapped from {len(PEAK_CONCURRENT)} old labels)")
    if skipped_mapping:
        print(f"{YELLOW}Unmapped old labels: {len(skipped_mapping)}{NC}")

    if args.rounds > 1:
        return _run_multi(runners, targets, daemonsets, skipped_mapping, args)

    # Single run
    result = run_simulation(runners, targets, daemonsets, seed=args.seed, threshold=args.threshold)
    for label, reason in skipped_mapping.items():
        result.skipped_labels[label] = reason

    utilization = compute_utilization(result)
    print_results(result, utilization)
    return 0


if __name__ == "__main__":
    sys.exit(main())
