"""Load workload CSV rows into Job objects with runner-pod overhead attached."""

from __future__ import annotations

import csv
import datetime as dt
import random
import sys
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts" / "python"))

from analyze_node_utilization import HOOKS_OVERHEAD_CPU_M as FALLBACK_HOOKS_CPU_M  # noqa: E402
from analyze_node_utilization import HOOKS_OVERHEAD_MEM_MI as FALLBACK_HOOKS_MEM_MI  # noqa: E402
from build_csv import build_label_table  # noqa: E402
from sim_nodes import Job  # noqa: E402

BUCKET_SEC = 300

RUNNER_POD_POOL = "c7i-runner"
RUNNER_POD_LABEL = "runner-pod"

FALLBACK_RUNNER_CPU_M = 750
FALLBACK_RUNNER_MEM_MI = 1024


def _bucket(ts: dt.datetime) -> int:
    return (int(ts.timestamp()) // BUCKET_SEC) * BUCKET_SEC


def _load_runner_overhead() -> tuple[int, int, int, int]:
    """Return (workflow_extra_cpu_m, workflow_extra_mem_mi, runner_cpu_m, runner_mem_mi)."""
    try:
        from runner_overhead import load_runner_pod_overhead

        overhead = load_runner_pod_overhead(REPO_ROOT)
        return (
            overhead.workflow_extra_cpu_m or FALLBACK_HOOKS_CPU_M,
            overhead.workflow_extra_mem_mi or FALLBACK_HOOKS_MEM_MI,
            overhead.runner_cpu_m,
            overhead.runner_mem_mi,
        )
    except Exception as e:
        print(
            f"warning: falling back to hooks/runner constants ({type(e).__name__}: {e})",
            file=sys.stderr,
        )
        return (
            FALLBACK_HOOKS_CPU_M,
            FALLBACK_HOOKS_MEM_MI,
            FALLBACK_RUNNER_CPU_M,
            FALLBACK_RUNNER_MEM_MI,
        )


def load_jobs(
    csv_path: Path,
    add_runner_pods: bool = True,
    runner_pool: str = RUNNER_POD_POOL,
    drop_providers: set[str] | None = None,
    keep_fraction: float = 1.0,
    keep_seed: int = 12345,
    last_days: int | None = None,
) -> list[Job]:
    if drop_providers is None:
        drop_providers = set()
    if not (0.0 < keep_fraction <= 1.0):
        raise ValueError(f"keep_fraction must be in (0, 1], got {keep_fraction}")
    rng = random.Random(keep_seed)  # noqa: S311

    label_table = build_label_table()
    hooks_cpu, hooks_mem, runner_cpu, runner_mem = _load_runner_overhead()

    jobs: list[Job] = []
    dropped_provider = 0
    dropped_downsample = 0
    dropped_unknown_label: dict[str, int] = defaultdict(int)
    with csv_path.open() as fh:
        reader = csv.DictReader(fh)
        has_provider = "provider" in (reader.fieldnames or [])
        for row in reader:
            if has_provider and row["provider"] in drop_providers:
                dropped_provider += 1
                continue
            if keep_fraction < 1.0 and rng.random() >= keep_fraction:
                dropped_downsample += 1
                continue
            label = row["label"]
            spec = label_table.get(label)
            if spec is None:
                dropped_unknown_label[label] += 1
                continue
            st = dt.datetime.fromisoformat(row["start_time"])
            en = dt.datetime.fromisoformat(row["end_time"])
            sb = _bucket(st)
            eb = _bucket(en)
            cpu_m = int(spec["vcpu"] * 1000) + hooks_cpu
            mem_mi = int(spec["memory_gib"] * 1024) + hooks_mem
            gpu = int(spec["gpu"])
            jobs.append(
                Job(
                    label=label,
                    pool=spec["nodepool"],
                    cpu_m=cpu_m,
                    mem_mi=mem_mi,
                    gpu=gpu,
                    start_bucket=sb,
                    end_bucket=eb,
                )
            )
            if add_runner_pods:
                jobs.append(
                    Job(
                        label=RUNNER_POD_LABEL,
                        pool=runner_pool,
                        cpu_m=runner_cpu,
                        mem_mi=runner_mem,
                        gpu=0,
                        start_bucket=sb,
                        end_bucket=eb,
                    )
                )
    if dropped_provider:
        print(f"  dropped by provider filter: {dropped_provider:,}", file=sys.stderr)
    if dropped_downsample:
        print(f"  dropped by downsample:      {dropped_downsample:,}", file=sys.stderr)
    if dropped_unknown_label:
        total = sum(dropped_unknown_label.values())
        print(
            f"  dropped unknown labels:     {total:,} ({len(dropped_unknown_label)} distinct)",
            file=sys.stderr,
        )
    if last_days is not None and last_days > 0 and jobs:
        max_start = max(j.start_bucket for j in jobs)
        cutoff = max_start - last_days * 86400
        before = len(jobs)
        jobs = [j for j in jobs if j.start_bucket >= cutoff]
        dropped = before - len(jobs)
        print(
            f"  filtered to last {last_days} days: {len(jobs):,} jobs kept, {dropped:,} dropped",
            file=sys.stderr,
        )
    return jobs
