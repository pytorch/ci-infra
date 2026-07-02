#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6.0"]
# ///
"""Phase 0 measurement harness for the node-size-sweep optimizer.

Runs three benchmarks:
  1. Single-run wall-clock (excl CSV load).
  2. Noise-floor: N seeds, stddev of opt_max, cal_cpu, cal_mem.
  3. Sim-vs-prod calibration: prod-parity flags, report cal_cpu / cal_mem.

Loads the CSV once, calls simulate() in-process for every run.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "python"))

import sim_load  # noqa: E402
import simulate as sim_mod  # noqa: E402
from sim_nodes import ClusterModel  # noqa: E402


def _cluster_totals(sim_out: dict) -> dict[str, float]:
    """Cluster-wide opt_cpu, opt_mem, cal_cpu, cal_mem averaged over buckets.

    opt = workload / (allocatable_raw + ds)                (ranking metric, D1)
    cal = cpu_used_m / cpu_alloc_m from per_pool           (prod PromQL match)

    opt numerator is workload only (excludes DaemonSets and phantom pods —
    phantom is a scheduling artifact, not real work). Denominator is
    post-kubelet, pre-DS — physical space if DS were zero.

    cal reuses the sim's own per_pool cpu_used_m / cpu_alloc_m, which include
    DS and phantom-pod load when the corresponding sim flags are on. This is
    the same aggregation that sim_report.report() prints as "matches prod
    PromQL", so the two numbers must agree to within rounding.
    """
    opt_cpu_series: list[float] = []
    opt_mem_series: list[float] = []
    cal_cpu_series: list[float] = []
    cal_mem_series: list[float] = []
    for _t, per_pool in sim_out["per_bucket"]:
        w_cpu = ds_cpu = a_cpu = 0
        w_mem = ds_mem = a_mem = 0
        used_cpu = alloc_cpu = 0
        used_mem = alloc_mem = 0
        for sums in per_pool.values():
            w_cpu += sums["workload_cpu_m"]
            ds_cpu += sums["ds_cpu_m"]
            a_cpu += sums["alloc_cpu_m_raw"]
            w_mem += sums["workload_mem_mi"]
            ds_mem += sums["ds_mem_mi"]
            a_mem += sums["alloc_mem_mi_raw"]
            used_cpu += sums["cpu_used_m"]
            alloc_cpu += sums["cpu_alloc_m"]
            used_mem += sums["mem_used_mi"]
            alloc_mem += sums["mem_alloc_mi"]
        opt_denom_cpu = a_cpu + ds_cpu
        opt_denom_mem = a_mem + ds_mem
        if opt_denom_cpu > 0:
            opt_cpu_series.append(w_cpu / opt_denom_cpu)
        if opt_denom_mem > 0:
            opt_mem_series.append(w_mem / opt_denom_mem)
        if alloc_cpu > 0:
            cal_cpu_series.append(used_cpu / alloc_cpu)
        if alloc_mem > 0:
            cal_mem_series.append(used_mem / alloc_mem)

    def _mean(xs: list[float]) -> float:
        return statistics.mean(xs) if xs else 0.0

    opt_cpu = _mean(opt_cpu_series)
    opt_mem = _mean(opt_mem_series)
    return {
        "opt_cpu": opt_cpu,
        "opt_mem": opt_mem,
        "opt_max": max(opt_cpu, opt_mem),
        "cal_cpu": _mean(cal_cpu_series),
        "cal_mem": _mean(cal_mem_series),
    }


def _per_fleet_calibration(sim_out: dict) -> dict[str, dict[str, float]]:
    """Per-pool cal_cpu / cal_mem averaged over buckets.

    Uses the same cpu_used_m / cpu_alloc_m aggregation as the cluster-wide
    cal metric, but keyed by pool (fleet) name.
    """
    per_pool_cpu: dict[str, list[float]] = {}
    per_pool_mem: dict[str, list[float]] = {}
    for _t, per_pool in sim_out["per_bucket"]:
        for name, sums in per_pool.items():
            if sums["cpu_alloc_m"] > 0:
                per_pool_cpu.setdefault(name, []).append(sums["cpu_used_m"] / sums["cpu_alloc_m"])
            if sums["mem_alloc_mi"] > 0:
                per_pool_mem.setdefault(name, []).append(sums["mem_used_mi"] / sums["mem_alloc_mi"])
    out: dict[str, dict[str, float]] = {}
    fleets = sorted(set(per_pool_cpu) | set(per_pool_mem))
    for name in fleets:
        cpu_vals = per_pool_cpu.get(name, [])
        mem_vals = per_pool_mem.get(name, [])
        out[name] = {
            "cal_cpu": statistics.mean(cpu_vals) if cpu_vals else 0.0,
            "cal_mem": statistics.mean(mem_vals) if mem_vals else 0.0,
        }
    return out


def _run_sim(jobs, seed: int, **overrides) -> dict:
    """Fresh ClusterModel per call — cheap, avoids any cross-call caching concerns."""
    return sim_mod.simulate(
        jobs,
        model=ClusterModel(),
        seed=seed,
        progress=False,
        **overrides,
    )


def bench_single_run(jobs) -> dict:
    t0 = time.perf_counter()
    sim_out = _run_sim(jobs, seed=42)
    wall = time.perf_counter() - t0
    metrics = _cluster_totals(sim_out)
    return {"wall_clock_s": wall, "metrics": metrics}


def bench_noise_floor(jobs, n_seeds: int) -> dict:
    per_seed: list[dict] = []
    t0 = time.perf_counter()
    for i in range(1, n_seeds + 1):
        r_t0 = time.perf_counter()
        sim_out = _run_sim(jobs, seed=i)
        m = _cluster_totals(sim_out)
        elapsed = time.perf_counter() - r_t0
        per_seed.append({"seed": i, "wall_s": elapsed, **m})
        print(
            f"  seed {i:>2}/{n_seeds}  wall={elapsed:6.1f}s  "
            f"opt_max={m['opt_max']:.4f}  cal_cpu={m['cal_cpu']:.4f}  cal_mem={m['cal_mem']:.4f}",
            file=sys.stderr,
        )
    total_wall = time.perf_counter() - t0

    def _stats(key: str) -> dict[str, float]:
        vals = [r[key] for r in per_seed]
        mean = statistics.mean(vals)
        stddev = statistics.stdev(vals) if len(vals) >= 2 else 0.0
        return {"mean": mean, "stddev": stddev, "rel_stddev": (stddev / mean) if mean else 0.0}

    return {
        "n_seeds": n_seeds,
        "total_wall_s": total_wall,
        "opt_max": _stats("opt_max"),
        "opt_cpu": _stats("opt_cpu"),
        "opt_mem": _stats("opt_mem"),
        "cal_cpu": _stats("cal_cpu"),
        "cal_mem": _stats("cal_mem"),
        "per_seed": per_seed,
    }


def _run_calibration_with_csv(csv_path: Path) -> dict:
    prod_jobs = sim_load.load_jobs(
        csv_path,
        drop_providers={"lf"},
        keep_fraction=0.5,
    )
    t0 = time.perf_counter()
    sim_out = sim_mod.simulate(
        prod_jobs,
        model=ClusterModel(),
        seed=42,
        empty_ttl_buckets=1,
        daemonsets_in_metric=True,
        phantom_pods_enabled=True,
        progress=False,
    )
    wall = time.perf_counter() - t0
    metrics = _cluster_totals(sim_out)
    per_fleet = _per_fleet_calibration(sim_out)
    return {
        "wall_clock_s": wall,
        "n_jobs": len(prod_jobs),
        "flags": ("--daemonsets-in-metric --phantom-pods --empty-ttl-buckets 1 --drop-provider lf --keep-fraction 0.5"),
        "metrics": metrics,
        "per_fleet": per_fleet,
    }


def _pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--csv", default=str(HERE / "pytorch_60d.csv"), help="workload CSV path")
    ap.add_argument("--skip-noise-floor", action="store_true", help="skip Benchmark 2 (slow)")
    ap.add_argument("--n-seeds", type=int, default=20, help="seeds for noise-floor (default 20)")
    ap.add_argument("--output", default=str(HERE / "benchmark_results.json"), help="JSON output path")
    args = ap.parse_args()

    csv_path = Path(args.csv)
    if not csv_path.is_file():
        print(f"error: CSV not found: {csv_path}", file=sys.stderr)
        return 1

    print(f"loading {csv_path}...", file=sys.stderr)
    t0 = time.perf_counter()
    jobs = sim_load.load_jobs(csv_path)
    csv_load_s = time.perf_counter() - t0
    print(f"  {len(jobs):,} pod-lifetimes loaded in {csv_load_s:.1f}s", file=sys.stderr)

    results: dict = {
        "csv_path": str(csv_path),
        "csv_load_s": csv_load_s,
        "n_jobs": len(jobs),
    }

    print("\n[bench 1/3] single-run wall-clock...", file=sys.stderr)
    b1 = bench_single_run(jobs)
    results["benchmark_1_single_run"] = b1
    print(
        f"  done: {b1['wall_clock_s']:.1f}s  opt_max={b1['metrics']['opt_max']:.4f}",
        file=sys.stderr,
    )

    if not args.skip_noise_floor:
        print(f"\n[bench 2/3] noise floor across {args.n_seeds} seeds...", file=sys.stderr)
        b2 = bench_noise_floor(jobs, n_seeds=args.n_seeds)
        results["benchmark_2_noise_floor"] = b2

    print("\n[bench 3/3] sim-vs-prod calibration...", file=sys.stderr)
    b3 = _run_calibration_with_csv(csv_path)
    results["benchmark_3_calibration"] = b3

    # Human report on stdout.
    print()
    print("==== Phase 0 measurements ====")
    print()
    print("Benchmark 1: single sim run wall-clock")
    print(f"  wall-clock (excl CSV load): {b1['wall_clock_s']:.1f}s")
    print(f"  CSV load:                   {csv_load_s:.1f}s")
    print(f"  total:                      {b1['wall_clock_s'] + csv_load_s:.1f}s")
    print(
        f"  metrics: opt_max={b1['metrics']['opt_max']:.4f} "
        f"opt_cpu={b1['metrics']['opt_cpu']:.4f} opt_mem={b1['metrics']['opt_mem']:.4f} "
        f"cal_cpu={b1['metrics']['cal_cpu']:.4f} cal_mem={b1['metrics']['cal_mem']:.4f}"
    )
    print()
    if not args.skip_noise_floor:
        b2 = results["benchmark_2_noise_floor"]
        print(f"Benchmark 2: noise floor across {b2['n_seeds']} seeds")
        for key in ("opt_max", "opt_cpu", "opt_mem", "cal_cpu", "cal_mem"):
            s = b2[key]
            print(f"  {key:<8}: mean={s['mean']:.4f}  stddev={s['stddev']:.4f} ({s['rel_stddev'] * 100:.2f}%)")
        print(f"  total wall: {b2['total_wall_s']:.1f}s ({b2['total_wall_s'] / b2['n_seeds']:.1f}s/seed)")
        print()
    print("Benchmark 3: sim-vs-prod calibration")
    print(f"  Config: {b3['flags']}")
    print(f"  n_jobs after filtering: {b3['n_jobs']:,}")
    print(f"  wall: {b3['wall_clock_s']:.1f}s")
    print(f"  cal_cpu: {_pct(b3['metrics']['cal_cpu'])}")
    print(f"  cal_mem: {_pct(b3['metrics']['cal_mem'])}")
    print('  --> Compare cal_cpu against Grafana "Total OSDC utilization over time"')
    print("      filtered to the same clusters+fleets and same time window.")
    print()

    per_fleet = b3.get("per_fleet") or {}
    if per_fleet:
        print("==== Per-fleet calibration (cal_cpu / cal_mem, matches prod dashboard) ====")
        print(f"  {'fleet':<16} {'cal_cpu':>9} {'cal_mem':>9}")
        for name in sorted(per_fleet):
            m = per_fleet[name]
            print(f"  {name:<16} {_pct(m['cal_cpu']):>9} {_pct(m['cal_mem']):>9}")
        print()

    out_path = Path(args.output)
    out_path.write_text(json.dumps(results, indent=2, default=str))
    print(f"raw results written to {out_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
