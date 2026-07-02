#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6.0"]
# ///
"""Replay a workload CSV onto a simulated cluster, bucket by bucket.

Step 3 (placeholder top-up) and step 4b (placeholder preemption) are gated by
``placeholders_enabled``; when False, jobs go directly from 4a to 4c.

Read a CSV of jobs (label, nodepool, nodepool_fraction, start_time, end_time),
join each label to its runner def (vcpu, memory, gpu, fleet, instance_type),
and simulate a Karpenter-style bin-packed cluster on real 3D
(cpu_millicores, memory_mib, gpu) allocatable capacity.

Per-node allocatable = instance_capacity - kubelet_reserved - daemonset_overhead.
Karpenter emulation: for each pod, pick the highest-weight instance in the
fleet whose allocatable fits the request.

Fresh nodes are modeled as `warming` for a per-pool window (default 5 min, GPU
10 min, baremetal 15 min) during which they cannot accept workload jobs.
Look-ahead pre-provisioning creates warming nodes for future arrivals so jobs
still start on-time via the normal placement path once the node flips to warm.

Model, per 5-min bucket iterated contiguously:
    0. Promote warming nodes whose warming_until_bucket <= bucket_idx.
    1. Expire placeholders older than PLACEHOLDER_MAX_AGE_BUCKETS (default 2).
    2. Deprovision finishers whose end_bucket == this bucket.
    2b. Look-ahead: create warming nodes for future arrivals that will not fit
        the currently-free capacity.
    3. Top up placeholders per (pool, cpu_m, mem_mi, gpu) to match arrivals.
       Each new placeholder is bin-packed via MostAllocated on free space
       (including warming nodes — placeholders are pause containers with no
       wait-for-hooks gate); creates a new node if no candidate has room.
    4. Place arrivals in shuffled order. For each job:
       a. MostAllocated on WARM nodes with truly-free 3D room.
       b. Else preempt a matching-shape placeholder on the MostAllocated
          warm node.
       c. Else create a new WARM node (unforecasted demand cannot be deferred).
    5. Deprovision same-bucket start=end finishers.
    6. Measure: allocatable-weighted CPU and memory util per pool + cluster,
       GPU util per pool where applicable.
    7. Consolidate: drop nodes empty >= KARPENTER_EMPTY_TTL_BUCKETS.

Every workload job in the CSV is paired with an ARC runner pod on the
c7i-runner fleet (750m/1Gi), same lifetime. Placeholders apply to both.

Usage:
    uv run simulate.py path/to/workload.csv --seed 42
"""

from __future__ import annotations

import argparse
import random
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts" / "python"))

from sim_load import RUNNER_POD_POOL, load_jobs  # noqa: E402
from sim_nodes import ClusterModel, Job, Node, Placeholder, fits, most_allocated_score  # noqa: E402
from sim_report import report  # noqa: E402

BUCKET_SEC = 300
KARPENTER_EMPTY_TTL_BUCKETS = 2
PLACEHOLDER_MAX_AGE_BUCKETS = 2

WARMUP_BUCKETS_DEFAULT = 1
WARMUP_BUCKETS_GPU = 2
WARMUP_BUCKETS_BAREMETAL = 3
GPU_POOL_PREFIXES = ("g5", "g6", "g4dn")
BAREMETAL_POOL_PREFIXES = ("p4d", "p5", "p6-b200")


def _pool_class(pool: str) -> str:
    for prefix in BAREMETAL_POOL_PREFIXES:
        if pool.startswith(prefix):
            return "baremetal"
    for prefix in GPU_POOL_PREFIXES:
        if pool.startswith(prefix):
            return "gpu"
    return "default"


def _warmup_buckets(pool: str, default: int, gpu: int, baremetal: int) -> int:
    cls = _pool_class(pool)
    if cls == "baremetal":
        return baremetal
    if cls == "gpu":
        return gpu
    return default


def _mark_empty_if_needed(node: Node, bucket_idx: int) -> None:
    if node.warming_until_bucket is not None:
        return
    if node.live_jobs == 0 and not node.placeholders:
        node.cpu_used_m = 0
        node.mem_used_mi = 0
        node.gpu_used = 0
        if node.empty_since_bucket is None:
            node.empty_since_bucket = bucket_idx


def _place_free(
    pool_nodes: list[Node], cpu_m: int, mem_mi: int, gpu: int, include_warming: bool = False
) -> Node | None:
    best: Node | None = None
    best_score = -1.0
    for n in pool_nodes:
        if not include_warming and n.warming_until_bucket is not None:
            continue
        if not fits(n, cpu_m, mem_mi, gpu):
            continue
        score = most_allocated_score(n)
        if score > best_score:
            best = n
            best_score = score
    return best


def _preempt_placeholder(
    pool_nodes: list[Node], cpu_m: int, mem_mi: int, gpu: int, include_warming: bool = False
) -> tuple[Node, Placeholder] | None:
    best: tuple[Node, Placeholder] | None = None
    best_score = -1.0
    for n in pool_nodes:
        if not include_warming and n.warming_until_bucket is not None:
            continue
        for p in n.placeholders:
            if p.cpu_m == cpu_m and p.mem_mi == mem_mi and p.gpu == gpu:
                score = most_allocated_score(n)
                if score > best_score:
                    best = (n, p)
                    best_score = score
                break
    return best


def _place_phantom(pool_nodes: list[Node], job: Job, cap: float) -> None:
    candidates = [n for n in pool_nodes if n.warming_until_bucket is None]
    candidates.sort(key=most_allocated_score)
    for n in candidates:
        if n.cpu_used_m + n.phantom_cpu_m + job.cpu_m > n.cpu_allocatable_m:
            continue
        if n.mem_used_mi + n.phantom_mem_mi + job.mem_mi > n.mem_allocatable_mi:
            continue
        if n.gpu_used + n.phantom_gpu + job.gpu > n.gpu_allocatable:
            continue
        cpu_basis = n.cpu_allocatable_m + n.daemonset_cpu_m
        mem_basis = n.mem_allocatable_mi + n.daemonset_mem_mi
        gpu_basis = n.gpu_allocatable + n.daemonset_gpu
        if cpu_basis > 0 and (n.phantom_cpu_m + job.cpu_m) / cpu_basis > cap:
            continue
        if mem_basis > 0 and (n.phantom_mem_mi + job.mem_mi) / mem_basis > cap:
            continue
        if gpu_basis > 0 and (n.phantom_gpu + job.gpu) / gpu_basis > cap:
            continue
        n.phantom_cpu_m += job.cpu_m
        n.phantom_mem_mi += job.mem_mi
        n.phantom_gpu += job.gpu
        return


def simulate(
    jobs: list[Job],
    model: ClusterModel,
    seed: int,
    empty_ttl_buckets: int = KARPENTER_EMPTY_TTL_BUCKETS,
    placeholder_max_age: int = PLACEHOLDER_MAX_AGE_BUCKETS,
    warmup_buckets_default: int = WARMUP_BUCKETS_DEFAULT,
    warmup_buckets_gpu: int = WARMUP_BUCKETS_GPU,
    warmup_buckets_baremetal: int = WARMUP_BUCKETS_BAREMETAL,
    placeholders_enabled: bool = True,
    daemonsets_in_metric: bool = False,
    phantom_pods_enabled: bool = False,
    phantom_lookahead_buckets: int = 1,
    phantom_cap: float = 0.30,
    progress: bool = True,
) -> dict:
    rng = random.Random(seed)  # noqa: S311

    def warmup_for(pool: str) -> int:
        return _warmup_buckets(pool, warmup_buckets_default, warmup_buckets_gpu, warmup_buckets_baremetal)

    arrivals: dict[int, list[Job]] = defaultdict(list)
    for j in jobs:
        arrivals[j.start_bucket].append(j)

    t_first = min(j.start_bucket for j in jobs)
    t_last = max(j.end_bucket for j in jobs)
    total_buckets = (t_last - t_first) // BUCKET_SEC + 1

    pools: dict[str, list[Node]] = defaultdict(list)
    finishers: dict[int, list[tuple[Job, Node]]] = defaultdict(list)
    per_bucket: list[tuple[int, dict[str, dict]]] = []
    pool_max_nodes: dict[str, int] = defaultdict(int)
    pool_max_warming: dict[str, int] = defaultdict(int)
    pool_total_created: dict[str, int] = defaultdict(int)
    pool_total_placeholders: dict[str, int] = defaultdict(int)
    pool_total_preempted: dict[str, int] = defaultdict(int)
    pool_total_expired: dict[str, int] = defaultdict(int)

    max_wu = max(warmup_buckets_default, warmup_buckets_gpu, warmup_buckets_baremetal)

    bucket_idx = 0
    for t in range(t_first, t_last + BUCKET_SEC, BUCKET_SEC):
        # 0. Promote warming nodes.
        for ns in pools.values():
            for n in ns:
                if n.warming_until_bucket is not None and bucket_idx >= n.warming_until_bucket:
                    n.warming_until_bucket = None
                    _mark_empty_if_needed(n, bucket_idx)

        # 1. Expire placeholders.
        for name, ns in pools.items():
            for n in ns:
                kept: list[Placeholder] = []
                for p in n.placeholders:
                    if bucket_idx - p.created_bucket >= placeholder_max_age:
                        n.cpu_used_m -= p.cpu_m
                        n.mem_used_mi -= p.mem_mi
                        n.gpu_used -= p.gpu
                        pool_total_expired[name] += 1
                    else:
                        kept.append(p)
                n.placeholders = kept
                _mark_empty_if_needed(n, bucket_idx)

        # 2. Deprovision finishers.
        for j, node in finishers.pop(t, ()):
            node.cpu_used_m -= j.cpu_m
            node.mem_used_mi -= j.mem_mi
            node.gpu_used -= j.gpu
            node.live_jobs -= 1
            _mark_empty_if_needed(node, bucket_idx)

        # 2b. Look-ahead pre-provisioning per future pod.
        if max_wu > 0:
            pool_future: dict[str, list[Job]] = defaultdict(list)
            for offset in range(1, max_wu + 1):
                future_t = t + offset * BUCKET_SEC
                for fj in arrivals.get(future_t, ()):
                    if offset > warmup_for(fj.pool):
                        continue
                    pool_future[fj.pool].append(fj)
            for pool, fjs in pool_future.items():
                free_cpu = sum(n.cpu_allocatable_m - n.cpu_used_m for n in pools[pool])
                free_mem = sum(n.mem_allocatable_mi - n.mem_used_mi for n in pools[pool])
                free_gpu = sum(n.gpu_allocatable - n.gpu_used for n in pools[pool])
                for fj in fjs:
                    if free_cpu >= fj.cpu_m and free_mem >= fj.mem_mi and free_gpu >= fj.gpu:
                        free_cpu -= fj.cpu_m
                        free_mem -= fj.mem_mi
                        free_gpu -= fj.gpu
                        continue
                    node = model.make_node(pool, fj.cpu_m, fj.mem_mi, fj.gpu)
                    node.warming_until_bucket = bucket_idx + warmup_for(pool)
                    pools[pool].append(node)
                    pool_total_created[pool] += 1
                    free_cpu += node.cpu_allocatable_m - fj.cpu_m
                    free_mem += node.mem_allocatable_mi - fj.mem_mi
                    free_gpu += node.gpu_allocatable - fj.gpu

        # 3. Top up placeholders per (pool, cpu_m, mem_mi, gpu).
        # Snapshot arrivals for this bucket into a local list so shuffling in
        # step 4 does not mutate the arrivals dict — keeps simulate() safe to
        # call repeatedly in the same process with the same jobs.
        arr = list(arrivals.get(t, ()))
        if placeholders_enabled:
            needed: dict[tuple[str, int, int, int], int] = defaultdict(int)
            for j in arr:
                needed[(j.pool, j.cpu_m, j.mem_mi, j.gpu)] += 1
            for (pool, cpu_m, mem_mi, gpu), n_needed in needed.items():
                have = 0
                for node in pools[pool]:
                    for p in node.placeholders:
                        if p.cpu_m == cpu_m and p.mem_mi == mem_mi and p.gpu == gpu:
                            have += 1
                to_create = n_needed - have
                if to_create <= 0:
                    continue
                for _ in range(to_create):
                    node = _place_free(pools[pool], cpu_m, mem_mi, gpu, include_warming=True)
                    if node is None:
                        wu = warmup_for(pool)
                        node = model.make_node(pool, cpu_m, mem_mi, gpu)
                        node.warming_until_bucket = (bucket_idx + wu) if wu > 0 else None
                        pools[pool].append(node)
                        pool_total_created[pool] += 1
                    node.cpu_used_m += cpu_m
                    node.mem_used_mi += mem_mi
                    node.gpu_used += gpu
                    node.placeholders.append(
                        Placeholder(cpu_m=cpu_m, mem_mi=mem_mi, gpu=gpu, created_bucket=bucket_idx)
                    )
                    node.empty_since_bucket = None
                    pool_total_placeholders[pool] += 1

        # 4. Place arrivals in shuffled order.
        rng.shuffle(arr)
        for j in arr:
            # (a) Truly-free room on a warm node.
            node = _place_free(pools[j.pool], j.cpu_m, j.mem_mi, j.gpu)
            if node is not None:
                node.cpu_used_m += j.cpu_m
                node.mem_used_mi += j.mem_mi
                node.gpu_used += j.gpu
                node.live_jobs += 1
                node.empty_since_bucket = None
                finishers[j.end_bucket].append((j, node))
                continue
            # (b) Preempt a matching-shape placeholder on a warm node.
            picked = _preempt_placeholder(pools[j.pool], j.cpu_m, j.mem_mi, j.gpu) if placeholders_enabled else None
            if picked is not None:
                node, ph = picked
                node.placeholders.remove(ph)
                pool_total_preempted[j.pool] += 1
                node.live_jobs += 1
                node.empty_since_bucket = None
                finishers[j.end_bucket].append((j, node))
                continue
            # (c) Fresh warm node — unforecasted arrival cannot be deferred.
            node = model.make_node(j.pool, j.cpu_m, j.mem_mi, j.gpu)
            pools[j.pool].append(node)
            pool_total_created[j.pool] += 1
            node.cpu_used_m += j.cpu_m
            node.mem_used_mi += j.mem_mi
            node.gpu_used += j.gpu
            node.live_jobs += 1
            finishers[j.end_bucket].append((j, node))

        # 5. Deprovision same-bucket start=end finishers.
        for j, node in finishers.pop(t, ()):
            node.cpu_used_m -= j.cpu_m
            node.mem_used_mi -= j.mem_mi
            node.gpu_used -= j.gpu
            node.live_jobs -= 1
            _mark_empty_if_needed(node, bucket_idx)

        # 5b. Phantom load: pre-count next-bucket arrivals on candidate warm nodes.
        if phantom_pods_enabled:
            for ns in pools.values():
                for n in ns:
                    n.phantom_cpu_m = 0
                    n.phantom_mem_mi = 0
                    n.phantom_gpu = 0
            for offset in range(1, phantom_lookahead_buckets + 1):
                for pj in arrivals.get(t + offset * BUCKET_SEC, ()):
                    _place_phantom(pools[pj.pool], pj, phantom_cap)

        # 6. Measure.
        ds_factor = 1 if daemonsets_in_metric else 0
        per_pool: dict[str, dict] = {}
        for name, ns in pools.items():
            if not ns:
                continue
            cpu_used = sum(n.cpu_used_m + ds_factor * n.daemonset_cpu_m + n.phantom_cpu_m for n in ns)
            cpu_alloc = sum(n.cpu_allocatable_m + ds_factor * n.daemonset_cpu_m for n in ns)
            mem_used = sum(n.mem_used_mi + ds_factor * n.daemonset_mem_mi + n.phantom_mem_mi for n in ns)
            mem_alloc = sum(n.mem_allocatable_mi + ds_factor * n.daemonset_mem_mi for n in ns)
            gpu_used = sum(n.gpu_used + ds_factor * n.daemonset_gpu + n.phantom_gpu for n in ns)
            gpu_alloc = sum(n.gpu_allocatable + ds_factor * n.daemonset_gpu for n in ns)
            workload_cpu_m = sum(n.cpu_used_m for n in ns)
            workload_mem_mi = sum(n.mem_used_mi for n in ns)
            workload_gpu = sum(n.gpu_used for n in ns)
            ds_cpu_m = sum(n.daemonset_cpu_m for n in ns)
            ds_mem_mi = sum(n.daemonset_mem_mi for n in ns)
            ds_gpu = sum(n.daemonset_gpu for n in ns)
            alloc_cpu_m_raw = sum(n.cpu_allocatable_m for n in ns)
            alloc_mem_mi_raw = sum(n.mem_allocatable_mi for n in ns)
            alloc_gpu_raw = sum(n.gpu_allocatable for n in ns)
            per_pool[name] = {
                "cpu_used_m": cpu_used,
                "cpu_alloc_m": cpu_alloc,
                "mem_used_mi": mem_used,
                "mem_alloc_mi": mem_alloc,
                "gpu_used": gpu_used,
                "gpu_alloc": gpu_alloc,
                "workload_cpu_m": workload_cpu_m,
                "workload_mem_mi": workload_mem_mi,
                "workload_gpu": workload_gpu,
                "ds_cpu_m": ds_cpu_m,
                "ds_mem_mi": ds_mem_mi,
                "ds_gpu": ds_gpu,
                "alloc_cpu_m_raw": alloc_cpu_m_raw,
                "alloc_mem_mi_raw": alloc_mem_mi_raw,
                "alloc_gpu_raw": alloc_gpu_raw,
            }
            if len(ns) > pool_max_nodes[name]:
                pool_max_nodes[name] = len(ns)
            warming_count = sum(1 for n in ns if n.warming_until_bucket is not None)
            if warming_count > pool_max_warming[name]:
                pool_max_warming[name] = warming_count

        per_bucket.append((t, per_pool))

        # 7. Consolidate.
        for name in list(pools):
            kept_nodes = []
            for n in pools[name]:
                if n.empty_since_bucket is not None and bucket_idx - n.empty_since_bucket >= empty_ttl_buckets:
                    continue
                kept_nodes.append(n)
            if kept_nodes:
                pools[name] = kept_nodes
            else:
                del pools[name]

        if progress and bucket_idx % 1000 == 0:
            live = sum(len(ns) for ns in pools.values())
            print(
                f"  bucket {bucket_idx:>6}/{total_buckets}  "
                f"{datetime.fromtimestamp(t, UTC):%Y-%m-%d %H:%M}  nodes={live:>5}",
                file=sys.stderr,
            )
        bucket_idx += 1

    return {
        "per_bucket": per_bucket,
        "pool_max_nodes": dict(pool_max_nodes),
        "pool_max_warming": dict(pool_max_warming),
        "pool_total_created": dict(pool_total_created),
        "pool_total_placeholders": dict(pool_total_placeholders),
        "pool_total_preempted": dict(pool_total_preempted),
        "pool_total_expired": dict(pool_total_expired),
        "flags": {
            "daemonsets_in_metric": daemonsets_in_metric,
            "phantom_pods_enabled": phantom_pods_enabled,
            "phantom_lookahead_buckets": phantom_lookahead_buckets,
            "phantom_cap": phantom_cap,
            "placeholders_enabled": placeholders_enabled,
            "empty_ttl_buckets": empty_ttl_buckets,
            "placeholder_max_age": placeholder_max_age,
            "warmup_buckets_default": warmup_buckets_default,
            "warmup_buckets_gpu": warmup_buckets_gpu,
            "warmup_buckets_baremetal": warmup_buckets_baremetal,
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("csv", help="workload CSV produced by build_csv.py extract")
    ap.add_argument("--seed", type=int, default=42, help="RNG seed for arrival shuffle")
    ap.add_argument(
        "--empty-ttl-buckets",
        type=int,
        default=KARPENTER_EMPTY_TTL_BUCKETS,
        help="drop nodes empty for >= this many consecutive 5min buckets (default 2 = 10min)",
    )
    ap.add_argument(
        "--placeholder-max-age",
        type=int,
        default=PLACEHOLDER_MAX_AGE_BUCKETS,
        help="expire placeholders at start of the bucket that's >= this many buckets after creation",
    )
    ap.add_argument(
        "--no-runner-pods",
        action="store_true",
        help=f"disable modeling the ARC runner-pod overhead on the {RUNNER_POD_POOL} fleet",
    )
    ap.add_argument("--runner-pod-pool", default=RUNNER_POD_POOL, help="pool name for runner-pod overhead")
    ap.add_argument(
        "--drop-provider",
        action="append",
        default=[],
        choices=["mt", "lf"],
        help="drop all rows with this provider (repeatable)",
    )
    ap.add_argument(
        "--keep-fraction",
        type=float,
        default=1.0,
        help="deterministically keep this fraction of rows (0 < f <= 1)",
    )
    ap.add_argument(
        "--no-warmup",
        action="store_true",
        help="disable the warming-node model: fresh nodes are instantly warm",
    )
    ap.add_argument(
        "--no-placeholders",
        action="store_true",
        help="disable ARC placeholder pods entirely (step 3 skipped, step 4b never fires)",
    )
    ap.add_argument("--warmup-buckets", type=int, default=WARMUP_BUCKETS_DEFAULT)
    ap.add_argument("--warmup-buckets-gpu", type=int, default=WARMUP_BUCKETS_GPU)
    ap.add_argument("--warmup-buckets-baremetal", type=int, default=WARMUP_BUCKETS_BAREMETAL)
    ap.add_argument(
        "--daemonsets-in-metric",
        action="store_true",
        help="include DaemonSet requests in numerator+denominator (matches prod node_compactor_node_utilization_ratio)",
    )
    ap.add_argument(
        "--phantom-pods",
        action="store_true",
        help="pre-count next-bucket arrivals as phantom load on warm nodes (matches prod compactor)",
    )
    ap.add_argument(
        "--phantom-lookahead-buckets",
        type=int,
        default=1,
        help="how many future buckets of arrivals to phantom-load (default 1 = next bucket only)",
    )
    ap.add_argument(
        "--phantom-cap",
        type=float,
        default=0.30,
        help="max fraction of node allocatable consumed by phantoms (default 0.30 matches prod PHANTOM_LOAD_CAP)",
    )
    ap.add_argument("--no-progress", action="store_true")
    ap.add_argument(
        "--last-days",
        type=int,
        default=None,
        help="filter jobs to those starting within the last N days of the CSV's max start_bucket",
    )
    args = ap.parse_args()

    print(f"loading {args.csv}...", file=sys.stderr)
    jobs = load_jobs(
        Path(args.csv),
        add_runner_pods=not args.no_runner_pods,
        runner_pool=args.runner_pod_pool,
        drop_providers=set(args.drop_provider),
        keep_fraction=args.keep_fraction,
        last_days=args.last_days,
    )
    print(f"  {len(jobs):,} pod-lifetimes (including runner pods)", file=sys.stderr)

    if not jobs:
        print("no jobs - nothing to simulate", file=sys.stderr)
        return 1

    if args.no_warmup:
        wu_default = wu_gpu = wu_baremetal = 0
    else:
        wu_default = args.warmup_buckets
        wu_gpu = args.warmup_buckets_gpu
        wu_baremetal = args.warmup_buckets_baremetal

    placeholders_enabled = not args.no_placeholders
    print(
        f"simulating (seed={args.seed}, empty_ttl_buckets={args.empty_ttl_buckets}, "
        f"placeholder_max_age={args.placeholder_max_age}, "
        f"warmup=default:{wu_default}/gpu:{wu_gpu}/bm:{wu_baremetal} buckets, "
        f"placeholders={placeholders_enabled}, "
        f"daemonsets_in_metric={args.daemonsets_in_metric}, "
        f"phantom_pods={args.phantom_pods}"
        f"{f' (lookahead={args.phantom_lookahead_buckets}, cap={args.phantom_cap})' if args.phantom_pods else ''}"
        f")...",
        file=sys.stderr,
    )
    model = ClusterModel()
    sim = simulate(
        jobs,
        model=model,
        seed=args.seed,
        empty_ttl_buckets=args.empty_ttl_buckets,
        placeholder_max_age=args.placeholder_max_age,
        warmup_buckets_default=wu_default,
        warmup_buckets_gpu=wu_gpu,
        warmup_buckets_baremetal=wu_baremetal,
        placeholders_enabled=placeholders_enabled,
        daemonsets_in_metric=args.daemonsets_in_metric,
        phantom_pods_enabled=args.phantom_pods,
        phantom_lookahead_buckets=args.phantom_lookahead_buckets,
        phantom_cap=args.phantom_cap,
        progress=not args.no_progress,
    )

    report(sim)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
