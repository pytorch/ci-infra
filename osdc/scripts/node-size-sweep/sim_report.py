"""Reporting for the sweep simulator: aggregation + pretty-printed output."""

from __future__ import annotations

import statistics
from collections import defaultdict


def percentiles(vals: list[float], pcts: list[int]) -> dict[int, float]:
    if not vals:
        return {p: float("nan") for p in pcts}
    sv = sorted(vals)
    n = len(sv)
    return {p: sv[max(0, min(n - 1, round(p / 100 * (n - 1))))] for p in pcts}


def _weighted_series(per_bucket, used_key: str, alloc_key: str) -> list[float]:
    out = []
    for _, per_pool in per_bucket:
        num = 0
        den = 0
        for _name, sums in per_pool.items():
            num += sums[used_key]
            den += sums[alloc_key]
        if den > 0:
            out.append(num / den)
    return out


def _unweighted_series(per_bucket, used_key: str, alloc_key: str) -> list[float]:
    out = []
    for _, per_pool in per_bucket:
        ratios = []
        for _name, sums in per_pool.items():
            if sums[alloc_key] > 0:
                ratios.append(sums[used_key] / sums[alloc_key])
        if ratios:
            out.append(sum(ratios) / len(ratios))
    return out


def report(sim_out: dict) -> None:
    per_bucket = sim_out["per_bucket"]
    pool_max = sim_out["pool_max_nodes"]
    pool_max_warming = sim_out.get("pool_max_warming", {})
    pool_created = sim_out["pool_total_created"]
    pool_placeholders = sim_out["pool_total_placeholders"]
    pool_preempted = sim_out["pool_total_preempted"]
    pool_expired = sim_out["pool_total_expired"]
    flags = sim_out.get("flags", {})

    pcts = [10, 20, 30, 40, 50, 60, 70, 80, 90]

    print("==== Simulation flags ====")
    print(f"  daemonsets_in_metric: {'on' if flags.get('daemonsets_in_metric') else 'off'}")
    if flags.get("phantom_pods_enabled"):
        print(
            f"  phantom_pods: on(lookahead={flags.get('phantom_lookahead_buckets')}, "
            f"cap={flags.get('phantom_cap'):.2f})"
        )
    else:
        print("  phantom_pods: off")
    print(f"  placeholders: {'on' if flags.get('placeholders_enabled') else 'off'}")
    print(
        f"  warmup: default:{flags.get('warmup_buckets_default')}/"
        f"gpu:{flags.get('warmup_buckets_gpu')}/"
        f"baremetal:{flags.get('warmup_buckets_baremetal')}"
    )
    print(f"  empty_ttl_buckets: {flags.get('empty_ttl_buckets')}")
    print(f"  placeholder_max_age: {flags.get('placeholder_max_age')}")

    def _print_series(title: str, series: list[float]) -> None:
        print(f"\n==== Cluster-wide {title} ====")
        print(f"  buckets measured: {len(series):,}")
        if not series:
            print("  (no data)")
            return
        print(f"  average:          {statistics.mean(series):.1%}")
        p = percentiles(series, pcts)
        print("  " + "  ".join(f"p{q:>2}: {p[q]:.1%}" for q in pcts))

    weighted_label = (
        "allocatable-weighted, matches prod PromQL"
        if flags.get("daemonsets_in_metric")
        else "allocatable-weighted (excludes DaemonSets)"
    )
    _print_series(
        f"CPU utilization ({weighted_label})",
        _weighted_series(per_bucket, "cpu_used_m", "cpu_alloc_m"),
    )
    _print_series(
        "CPU utilization (unweighted mean of per-pool ratios)",
        _unweighted_series(per_bucket, "cpu_used_m", "cpu_alloc_m"),
    )
    _print_series(
        f"memory utilization ({weighted_label})",
        _weighted_series(per_bucket, "mem_used_mi", "mem_alloc_mi"),
    )
    _print_series(
        "memory utilization (unweighted mean of per-pool ratios)",
        _unweighted_series(per_bucket, "mem_used_mi", "mem_alloc_mi"),
    )

    pool_cpu: dict[str, list[float]] = defaultdict(list)
    pool_mem: dict[str, list[float]] = defaultdict(list)
    pool_gpu: dict[str, list[float]] = defaultdict(list)
    for _, per_pool in per_bucket:
        for name, sums in per_pool.items():
            if sums["cpu_alloc_m"] > 0:
                pool_cpu[name].append(sums["cpu_used_m"] / sums["cpu_alloc_m"])
            else:
                pool_cpu[name].append(0.0)
            if sums["mem_alloc_mi"] > 0:
                pool_mem[name].append(sums["mem_used_mi"] / sums["mem_alloc_mi"])
            else:
                pool_mem[name].append(0.0)
            if sums["gpu_alloc"] > 0:
                pool_gpu[name].append(sums["gpu_used"] / sums["gpu_alloc"])

    print("\n==== Per-pool utilization (placeholders included) ====")
    header = (
        f"  {'pool':<16} {'buckets':>8} {'max_nodes':>10} {'warming_max':>12} {'created':>8} "
        f"{'ph_new':>7} {'ph_pre':>7} {'ph_exp':>7} "
        f"{'cpu_avg':>8} {'cpu_p50':>8} {'cpu_p90':>8} "
        f"{'mem_avg':>8} {'mem_p50':>8} {'mem_p90':>8} "
        f"{'gpu_avg':>8} {'gpu_p50':>8} {'gpu_p90':>8}"
    )
    print(header)
    print("  " + "-" * (len(header) - 2))
    for name in sorted(pool_cpu, key=lambda n: -statistics.mean(pool_cpu[n])):
        cpu = pool_cpu[name]
        mem = pool_mem[name]
        gpu = pool_gpu.get(name, [])
        cp = percentiles(cpu, pcts)
        mp = percentiles(mem, pcts)
        gp = percentiles(gpu, pcts) if gpu else {50: float("nan"), 90: float("nan")}
        gpu_avg = statistics.mean(gpu) if gpu else float("nan")
        print(
            f"  {name:<16} {len(cpu):>8} {pool_max.get(name, 0):>10} {pool_max_warming.get(name, 0):>12} "
            f"{pool_created.get(name, 0):>8} "
            f"{pool_placeholders.get(name, 0):>7} {pool_preempted.get(name, 0):>7} {pool_expired.get(name, 0):>7} "
            f"{statistics.mean(cpu):>7.1%} {cp[50]:>7.1%} {cp[90]:>7.1%} "
            f"{statistics.mean(mem):>7.1%} {mp[50]:>7.1%} {mp[90]:>7.1%} "
            f"{gpu_avg:>7.1%} {gp[50]:>7.1%} {gp[90]:>7.1%}"
        )
