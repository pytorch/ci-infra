"""Load distribution logic for OSDC load tests.

Maps production runner traffic to OSDC runner types and computes
proportional job allocations for load testing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

# Maps old runner labels -> OSDC runner def names (without provider prefix).
# Source: docs/runner_naming_convention.md (Old Label -> New Label Mapping)
OLD_TO_OSDC_LABEL: dict[str, str] = {
    # x86 CPU -- Intel AVX-512 (c5, c7i families)
    "linux.large": "l-x86iavx512-2-4",
    "linux.c7i.large": "l-x86iavx512-2-4",
    "linux.2xlarge": "l-x86iavx512-8-16",
    "linux.c7i.2xlarge": "l-x86iavx512-8-16",
    "linux.4xlarge": "l-x86iavx512-16-32",
    "linux.4xlarge.for.testing.donotuse": "l-x86iavx512-16-32",
    "linux.c7i.4xlarge": "l-x86iavx512-16-32",
    "linux.9xlarge.ephemeral": "l-x86iavx512-37-68",
    "linux.12xlarge": "l-x86iavx512-46-85",
    "linux.12xlarge.ephemeral": "l-x86iavx512-46-85",
    "linux.c7i.12xlarge": "l-x86iavx512-46-85",
    "linux.24xlarge": "l-x86iavx512-94-192",
    "linux.24xlarge.ephemeral": "l-x86iavx512-94-192",
    "linux.c7i.24xlarge": "l-x86iavx512-94-192",
    # x86 CPU -- Intel AMX (bare-metal + flex)
    "linux.24xl.spr-metal": "l-bx86iamx-92-167",
    "linux.2xlarge.amx": "l-x86iamx-8-32",
    "linux.8xlarge.amx": "l-x86iamx-32-128",
    # x86 CPU -- Intel AVX2 (m4 family)
    "linux.2xlarge.avx2": "l-x86iavx2-8-32",
    "linux.10xlarge.avx2": "l-x86iavx2-40-160",
    # x86 CPU -- Memory-optimized (r5, r7i)
    "linux.r7i.2xlarge": "l-x86iavx512-8-64",
    "linux.r7i.4xlarge": "l-x86iavx512-16-128",
    "linux.r7i.8xlarge": "l-x86iavx512-32-256",
    "linux.r7i.12xlarge": "l-x86iavx512-48-384",
    "linux.2xlarge.memory": "l-x86iavx512-8-64",
    "linux.4xlarge.memory": "l-x86iavx512-16-128",
    "linux.8xlarge.memory": "l-x86iavx512-32-256",
    "linux.12xlarge.memory": "l-x86iavx512-48-384",
    "linux.12xlarge.memory.ephemeral": "l-x86iavx512-48-384",
    "linux.24xlarge.memory": "l-x86iavx512-94-768",
    # x86 CPU -- AMD
    "linux.24xlarge.amd": "l-x86aavx512-125-463",
    # GPU -- T4 (g4dn)
    "linux.4xlarge.nvidia.gpu": "l-x86iavx512-29-115-t4",
    "linux.g4dn.4xlarge.nvidia.gpu": "l-x86iavx512-29-115-t4",
    "linux.g4dn.12xlarge.nvidia.gpu": "l-x86iavx512-45-172-t4-4",
    "linux.g4dn.metal.nvidia.gpu": "l-bx86iavx512-94-344-t4-8",
    # GPU -- A10G (g5)
    "linux.g5.4xlarge.nvidia.gpu": "l-x86aavx2-29-113-a10g",
    "linux.g5.12xlarge.nvidia.gpu": "l-x86aavx2-45-167-a10g-4",
    "linux.g5.48xlarge.nvidia.gpu": "l-x86aavx2-189-704-a10g-8",
    # GPU -- L4 (g6)
    "linux.g6.4xlarge.experimental.nvidia.gpu": "l-x86aavx2-29-113-l4",
    "linux.g6.12xlarge.nvidia.gpu": "l-x86aavx2-45-172-l4-4",
    # ARM64 (Graviton)
    "linux.arm64.2xlarge": "l-arm64g2-6-32",
    "linux.arm64.2xlarge.ephemeral": "l-arm64g2-6-32",
    "linux.arm64.m7g.4xlarge": "l-arm64g3-16-62",
    "linux.arm64.m7g.4xlarge.ephemeral": "l-arm64g3-16-62",
    "linux.arm64.m8g.4xlarge": "l-arm64g4-16-62",
    "linux.arm64.m8g.4xlarge.ephemeral": "l-arm64g4-16-62",
    "linux.arm64.r7g.12xlarge.memory": "l-arm64g3-61-463",
    "linux.arm64.m7g.metal": "l-barm64g4-62-226",
}

# 30-day job counts per old label (pytorch/pytorch only).
# Source: docs/current_runner_load_distribution.md (queried 2026-03-18)
PRODUCTION_JOB_COUNTS: dict[str, int] = {
    "linux.2xlarge": 491_396,
    "linux.c7i.2xlarge": 350_911,
    "linux.4xlarge": 279_660,
    "linux.g5.4xlarge.nvidia.gpu": 93_759,
    "linux.g6.4xlarge.experimental.nvidia.gpu": 61_263,
    "linux.2xlarge.amx": 46_174,
    "linux.large": 37_043,
    "linux.12xlarge": 31_542,
    "linux.c7i.4xlarge": 29_111,
    "linux.arm64.m7g.4xlarge": 23_594,
    "linux.g4dn.metal.nvidia.gpu": 19_040,
    "linux.arm64.m8g.4xlarge": 17_935,
    "linux.g4dn.12xlarge.nvidia.gpu": 17_625,
    "linux.12xlarge.memory": 10_978,
    "linux.4xlarge.memory": 10_695,
    "linux.12xlarge.memory.ephemeral": 9_815,
    "linux.g5.12xlarge.nvidia.gpu": 7_932,
    "linux.9xlarge.ephemeral": 7_678,
    "linux.8xlarge.amx": 7_663,
    "linux.24xl.spr-metal": 6_983,
    "linux.r7i.2xlarge": 5_173,
    "linux.arm64.r7g.12xlarge.memory": 5_046,
    "linux.arm64.2xlarge": 4_552,
    "linux.24xlarge.memory": 3_652,
    "linux.g4dn.4xlarge.nvidia.gpu": 3_651,
    "linux.g5.48xlarge.nvidia.gpu": 3_465,
    "linux.g6.12xlarge.nvidia.gpu": 3_261,
    "linux.arm64.2xlarge.ephemeral": 1_536,
    "linux.8xlarge.memory": 1_522,
    "linux.r7i.4xlarge": 1_449,
    "linux.10xlarge.avx2": 1_290,
    "linux.arm64.m7g.metal": 1_170,
    "linux.c7i.12xlarge": 858,
    "linux.24xlarge.amd": 720,
    "linux.2xlarge.avx2": 534,
    "linux.4xlarge.nvidia.gpu": 468,
    "linux.2xlarge.memory": 15,
    "linux.24xlarge": 14,
}

# GPU type suffixes in OSDC runner labels
_OSDC_GPU_PATTERN = re.compile(r"-(t4|a10g|l4)(?:-(\d+))?$")


@dataclass
class RunnerAllocation:
    """A load test allocation for a single runner type."""

    osdc_label: str
    job_count: int
    source_job_count: int
    proportion: float
    is_gpu: bool
    is_arm64: bool
    gpu_count: int


def classify_runner(label: str) -> tuple[bool, bool, int]:
    """Classify a runner label. Returns (is_gpu, is_arm64, gpu_count)."""
    is_arm64 = "arm64" in label

    # OSDC GPU labels (e.g., l-x86iavx512-29-115-t4-4)
    m = _OSDC_GPU_PATTERN.search(label)
    if m:
        gpu_count = int(m.group(2)) if m.group(2) else 1
        return True, is_arm64, gpu_count

    return False, is_arm64, 0


def get_available_runners(
    upstream_dir: Path,
    root_dir: Path,
) -> set[str]:
    """Scan runner def YAML files and return the set of available runner labels."""
    labels: set[str] = set()

    def _scan_defs(defs_dir: Path) -> None:
        if not defs_dir.is_dir():
            return
        for f in defs_dir.glob("*.yaml"):
            data = yaml.safe_load(f.read_text())
            if data and "runner" in data:
                labels.add(data["runner"]["name"])

    # Upstream arc-runners
    _scan_defs(upstream_dir / "modules" / "arc-runners" / "defs")

    # Consumer arc-runners (overrides/additions)
    _scan_defs(root_dir / "modules" / "arc-runners" / "defs")

    return labels


def _aggregate_production_counts() -> dict[str, int]:
    """Aggregate old-label production job counts by OSDC label."""
    osdc_counts: dict[str, int] = {}
    for old_label, osdc_label in OLD_TO_OSDC_LABEL.items():
        count = PRODUCTION_JOB_COUNTS.get(old_label, 0)
        if count > 0:
            osdc_counts[osdc_label] = osdc_counts.get(osdc_label, 0) + count
    return osdc_counts


def compute_distribution(
    total_jobs: int,
    available_runners: set[str],
    min_jobs_per_type: int = 1,
) -> list[RunnerAllocation]:
    """Distribute total_jobs proportionally across available runner types.

    Uses the largest-remainder method (Hamilton's method) to ensure the
    allocated job counts sum to exactly total_jobs.
    """
    if not available_runners or total_jobs <= 0:
        return []

    osdc_counts = _aggregate_production_counts()

    # Build active set: only runners that are available, with their source counts
    active: dict[str, int] = {}
    for label in sorted(available_runners):
        active[label] = osdc_counts.get(label, 0)

    num_types = len(active)

    # Edge case: fewer jobs than runner types
    if total_jobs < num_types * min_jobs_per_type:
        result = []
        for i, label in enumerate(sorted(active)):
            is_gpu, is_arm64, gpu_count = classify_runner(label)
            total_source = sum(active.values())
            proportion = (
                active[label] / total_source if total_source > 0 else 1 / num_types
            )
            result.append(
                RunnerAllocation(
                    osdc_label=label,
                    job_count=1 if i < total_jobs else 0,
                    source_job_count=active[label],
                    proportion=proportion,
                    is_gpu=is_gpu,
                    is_arm64=is_arm64,
                    gpu_count=gpu_count,
                ),
            )
        return result

    total_source = sum(active.values())
    remaining = total_jobs - num_types * min_jobs_per_type

    # Compute raw proportional allocation
    raw: dict[str, float] = {}
    for label, count in active.items():
        if total_source > 0:
            raw[label] = min_jobs_per_type + remaining * (count / total_source)
        else:
            raw[label] = total_jobs / num_types

    # Largest-remainder method for exact rounding
    floored = {label: int(val) for label, val in raw.items()}
    remainders = {label: raw[label] - floored[label] for label in raw}
    diff = total_jobs - sum(floored.values())

    for label in sorted(remainders, key=remainders.get, reverse=True):
        if diff <= 0:
            break
        floored[label] += 1
        diff -= 1

    # Build result
    result = []
    for label in sorted(active):
        is_gpu, is_arm64, gpu_count = classify_runner(label)
        proportion = (
            active[label] / total_source if total_source > 0 else 1 / num_types
        )
        result.append(
            RunnerAllocation(
                osdc_label=label,
                job_count=floored[label],
                source_job_count=active[label],
                proportion=proportion,
                is_gpu=is_gpu,
                is_arm64=is_arm64,
                gpu_count=gpu_count,
            ),
        )

    return result
