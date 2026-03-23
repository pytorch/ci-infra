"""Generates GitHub Actions workflow YAML for OSDC load tests."""

from __future__ import annotations

import re

from distribution import RunnerAllocation

# Container images
CPU_CONTAINER = "ghcr.io/actions/actions-runner:latest"
GPU_CONTAINER = "nvidia/cuda:12.6.3-runtime-ubuntu22.04"

# GitHub Actions matrix max entries
MAX_MATRIX_SIZE = 256


def sanitize_job_key(label: str) -> str:
    """Sanitize a runner label for use as a GitHub Actions job key."""
    return re.sub(r"[^a-zA-Z0-9_-]", "-", label)


def generate_workflow(
    allocations: list[RunnerAllocation],
    prefix: str,
    cluster_id: str,
) -> str:
    """Generate a multi-job workflow YAML from runner allocations."""
    lines = [
        f"name: OSDC Load Test ({cluster_id})",
        "on:",
        "  pull_request:",
        "    branches: [main]",
        "",
        "concurrency:",
        f"  group: load-test-{cluster_id}",
        "  cancel-in-progress: true",
        "",
        "jobs:",
    ]

    for alloc in allocations:
        if alloc.job_count == 0:
            continue
        _write_job_blocks(lines, alloc, prefix)

    return "\n".join(lines) + "\n"


def _write_job_blocks(
    lines: list[str],
    alloc: RunnerAllocation,
    prefix: str,
) -> None:
    """Write one or more job blocks for an allocation (splits if >256)."""
    label = alloc.osdc_label
    indices = list(range(1, alloc.job_count + 1))

    if len(indices) <= MAX_MATRIX_SIZE:
        _write_single_job(lines, label, indices, alloc, prefix, suffix="")
    else:
        for chunk_idx, start in enumerate(range(0, len(indices), MAX_MATRIX_SIZE)):
            chunk = indices[start : start + MAX_MATRIX_SIZE]
            _write_single_job(
                lines,
                label,
                chunk,
                alloc,
                prefix,
                suffix=f"-part{chunk_idx}",
            )


def _write_single_job(
    lines: list[str],
    label: str,
    indices: list[int],
    alloc: RunnerAllocation,
    prefix: str,
    suffix: str,
) -> None:
    """Write a single job block to lines."""
    job_key = f"load-{sanitize_job_key(label)}{suffix}"
    runs_on = f"{prefix}{label}"
    container = GPU_CONTAINER if alloc.is_gpu else CPU_CONTAINER

    # Format matrix indices compactly
    index_str = ", ".join(str(i) for i in indices)

    lines.append(f"  {job_key}:")
    lines.append(f"    runs-on: {runs_on}")
    lines.append("    container:")
    lines.append(f"      image: {container}")
    lines.append("    strategy:")
    lines.append("      fail-fast: false")
    lines.append("      matrix:")
    lines.append(f"        index: [{index_str}]")
    lines.append("    steps:")

    # Architecture / GPU verification step
    if alloc.is_gpu:
        _write_gpu_verify_step(lines, alloc.gpu_count)
    elif alloc.is_arm64:
        _write_arch_verify_step(lines, "aarch64")
    else:
        _write_arch_verify_step(lines, "x86_64")

    # Checkout step (CPU/ARM only -- exercises git cache)
    if not alloc.is_gpu:
        lines.append("      - name: Checkout pytorch")
        lines.append("        uses: actions/checkout@v4")
        lines.append("        with:")
        lines.append("          repository: pytorch/pytorch")
        lines.append("          fetch-depth: 1")

    # Metadata reporting step
    lines.append("      - name: Report metadata")
    lines.append("        run: |")
    lines.append(f'          echo "runner_type={label}"')
    lines.append('          echo "matrix_index=${{ matrix.index }}"')
    lines.append('          echo "runner_name=${{ runner.name }}"')
    lines.append("")


def _write_arch_verify_step(lines: list[str], expected_arch: str) -> None:
    """Write an architecture verification step."""
    lines.append("      - name: Verify architecture")
    lines.append("        run: |")
    lines.append("          ARCH=$(uname -m)")
    lines.append('          echo "Architecture: $ARCH"')
    lines.append(f'          if [ "$ARCH" != "{expected_arch}" ]; then')
    lines.append(
        f'            echo "ERROR: Expected {expected_arch}, got $ARCH"',
    )
    lines.append("            exit 1")
    lines.append("          fi")


def _write_gpu_verify_step(lines: list[str], expected_gpu_count: int) -> None:
    """Write a GPU verification step."""
    lines.append("      - name: Verify GPU")
    lines.append("        run: |")
    lines.append("          nvidia-smi")
    lines.append("          GPU_COUNT=$(nvidia-smi -L | wc -l)")
    lines.append('          echo "GPU count: $GPU_COUNT"')
    lines.append(
        f'          if [ "$GPU_COUNT" -lt {expected_gpu_count} ]; then',
    )
    lines.append(
        f'            echo "ERROR: Expected >= {expected_gpu_count} GPUs, got $GPU_COUNT"',
    )
    lines.append("            exit 1")
    lines.append("          fi")
