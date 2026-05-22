#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6.0"]
# ///
"""Load runner / listener / workflow pod overhead from rendered runner YAMLs.

Reads the rendered Helm values + ConfigMap pair under
``modules/arc-runners*/generated/*.yaml`` and extracts the resource requests
of the three pods that participate in a runner: the runner pod (lands on the
dedicated ``c7i-runner`` fleet today), the listener pod (lands on system
nodes via the ``CriticalAddonsOnly`` toleration), and the workflow pod
template embedded in the ConfigMap (lands on the workflow fleet).

The loader exists so that ``analyze_node_utilization.py`` can stop hard-coding
``HOOKS_OVERHEAD_CPU_M`` / ``HOOKS_OVERHEAD_MEM_MI`` constants and read the
actual values that ship to the cluster. ``workflow_extra_*`` is exposed today
even though it is always 0 — a follow-up task will plug in dshm/sidecar
overhead without changing this loader's signature.
"""

from __future__ import annotations

import functools
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml
from analyze_node_utilization import parse_memory

# Single-source-of-truth: parse_cpu_millicores in daemonset_overhead handles
# every input case ("750m", "1", "1.5", 1, 0.5) and rejects negative values.
# Re-exported here as ``parse_cpu`` to keep the historical name on this
# module's public surface.
from daemonset_overhead import parse_cpu_millicores as parse_cpu


@dataclass(frozen=True)
class RunnerPodOverhead:
    """Resource overhead of the three pods that make up one runner.

    All generated runner YAMLs share the same template, so all runner pods,
    listener pods, and workflow-pod templates have identical resource
    requests. The loader reads the first generated file and warns (without
    failing) if any other file disagrees.

    Fields:
        runner_cpu_m / runner_mem_mi:
            Runner pod requests (lands on ``c7i-runner`` fleet today).
        listener_cpu_m / listener_mem_mi:
            Listener pod requests (lands on system nodes via the
            ``CriticalAddonsOnly`` toleration).
        workflow_extra_cpu_m / workflow_extra_mem_mi:
            Workflow-pod overhead BEYOND the def's vcpu / memory. Today
            this is 0/0 — the workflow pod requests exactly the def's
            resources. Exists as a struct field so the next task can plug
            in dshm / sidecar overhead without changing this signature.
    """

    runner_cpu_m: int
    runner_mem_mi: int
    listener_cpu_m: int
    listener_mem_mi: int
    workflow_extra_cpu_m: int
    workflow_extra_mem_mi: int


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _relpath(p: Path) -> str:
    """Return ``p`` relative to cwd, falling back to absolute on ValueError."""
    try:
        return os.path.relpath(p)
    except ValueError:
        return str(p)


def _container_requests(container: dict) -> tuple[int, int]:
    """Return ``(cpu_millicores, memory_mib)`` from a container's requests."""
    requests = container.get("resources", {}).get("requests", {})
    cpu = parse_cpu(requests["cpu"])
    mem = parse_memory(requests["memory"])
    return cpu, mem


def _env_value(env: list[dict], name: str, path: Path) -> str:
    """Return the ``value`` of the env var named ``name`` or raise ValueError.

    If the env entry exists but uses ``valueFrom`` (e.g. a ConfigMap or
    Secret reference) instead of a literal ``value``, raise ValueError so
    the caller can attach file-path context — silently dereferencing a
    runtime ``valueFrom`` is wrong here because the listener's baseline
    must be a literal at render time.
    """
    for entry in env:
        if entry.get("name") == name:
            if "value" not in entry:
                raise ValueError(f"{_relpath(path)}: listener env var {name} uses valueFrom; expected literal value")
            return str(entry["value"])
    raise KeyError(f"Environment variable {name!r} not found in listener spec")


def _parse_generated_file(path: Path) -> RunnerPodOverhead:
    """Parse a single rendered runner YAML into a RunnerPodOverhead.

    Doc 1 is the Helm values (with ``template`` and ``listenerTemplate``).
    Doc 2 is the ConfigMap whose ``data['job-pod.yaml']`` holds the workflow
    pod template as a nested YAML string.

    Wraps every per-file lookup in a try/except so any ``KeyError``,
    ``IndexError``, ``TypeError``, or ``AttributeError`` from a malformed
    YAML is re-raised as a ``ValueError`` that names the offending file.
    Without this, callers see a bare ``KeyError: 'cpu'`` with no clue
    which YAML in the multi-file walk is broken.
    """
    try:
        with open(path) as fh:
            docs = list(yaml.safe_load_all(fh))

        values_doc = docs[0]
        configmap_doc = docs[1]

        # Runner pod requests
        runner_container = values_doc["template"]["spec"]["containers"][0]
        runner_cpu_m, runner_mem_mi = _container_requests(runner_container)

        # Listener pod requests + CAPACITY_AWARE_WORKFLOW_* baseline env vars
        listener_container = values_doc["listenerTemplate"]["spec"]["containers"][0]
        listener_cpu_m, listener_mem_mi = _container_requests(listener_container)

        listener_env = listener_container.get("env", [])
        baseline_cpu_m = parse_cpu(_env_value(listener_env, "CAPACITY_AWARE_WORKFLOW_CPU", path))
        baseline_mem_mi = parse_memory(_env_value(listener_env, "CAPACITY_AWARE_WORKFLOW_MEMORY", path))

        # Workflow pod requests come from the nested YAML inside the ConfigMap.
        # An empty ``data['job-pod.yaml']`` (yaml.safe_load("") -> None) would
        # otherwise blow up later as ``TypeError: 'NoneType' object is not
        # subscriptable`` — catch it here with a precise message instead.
        job_pod_str = configmap_doc["data"]["job-pod.yaml"]
        job_pod = yaml.safe_load(job_pod_str)
        if job_pod is None:
            raise ValueError(f"{_relpath(path)}: doc 2 ConfigMap 'data.job-pod.yaml' is empty")
        workflow_container = job_pod["spec"]["containers"][0]
        workflow_cpu_m, workflow_mem_mi = _container_requests(workflow_container)
    except ValueError:
        # Already a precise, file-path-tagged error — let it through.
        raise
    except (KeyError, IndexError, TypeError, AttributeError) as e:
        raise ValueError(f"failed to parse runner overhead from {_relpath(path)}: {e}") from e

    # Clamp to >= 0 — the workflow pod should never request less than the
    # baseline, but if a future def gets that wrong we don't want negative
    # overhead leaking into downstream packing math.
    workflow_extra_cpu_m = max(0, workflow_cpu_m - baseline_cpu_m)
    workflow_extra_mem_mi = max(0, workflow_mem_mi - baseline_mem_mi)

    return RunnerPodOverhead(
        runner_cpu_m=runner_cpu_m,
        runner_mem_mi=runner_mem_mi,
        listener_cpu_m=listener_cpu_m,
        listener_mem_mi=listener_mem_mi,
        workflow_extra_cpu_m=workflow_extra_cpu_m,
        workflow_extra_mem_mi=workflow_extra_mem_mi,
    )


def _collect_generated_files(roots: list[Path]) -> list[Path]:
    """Return the sorted, dedup'd list of rendered runner YAMLs under roots."""
    seen: set[Path] = set()
    files: list[Path] = []
    for root in roots:
        for f in root.glob("modules/arc-runners*/generated/*.yaml"):
            resolved = f.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            files.append(resolved)
    files.sort(key=lambda p: str(p))
    return files


def _warn_if_disagrees(
    first_path: Path,
    first: RunnerPodOverhead,
    other_path: Path,
    other: RunnerPodOverhead,
) -> None:
    """Print a stderr warning for every field where ``other`` differs from ``first``."""
    for field in (
        "runner_cpu_m",
        "runner_mem_mi",
        "listener_cpu_m",
        "listener_mem_mi",
        "workflow_extra_cpu_m",
        "workflow_extra_mem_mi",
    ):
        first_val = getattr(first, field)
        other_val = getattr(other, field)
        if other_val != first_val:
            print(
                f"WARNING: {_relpath(other_path)} disagrees with {_relpath(first_path)} on "
                f"{field}: {other_val} vs {first_val}. Using value from {_relpath(first_path)}.",
                file=sys.stderr,
            )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


@functools.lru_cache(maxsize=8)
def load_runner_pod_overhead(
    upstream_dir: Path,
    consumer_root: Path | None = None,
) -> RunnerPodOverhead:
    """Load runner / listener / workflow overhead from rendered runner YAMLs.

    Walks ``<upstream_dir>/modules/arc-runners*/generated/*.yaml`` and, when
    ``consumer_root`` is provided and points at a different path, also walks
    ``<consumer_root>/modules/arc-runners*/generated/*.yaml``. Files are
    deduplicated by resolved path and processed in sorted order.

    The first file (alphabetical by absolute path) is the source of truth.
    Every subsequent file is parsed and any field that disagrees with the
    first triggers a stderr warning — the first file's value wins.

    Raises:
        RuntimeError: If no generated YAMLs are found under any root.
    """
    roots = [upstream_dir]
    if consumer_root is not None and consumer_root.resolve() != upstream_dir.resolve():
        roots.append(consumer_root)

    files = _collect_generated_files(roots)
    if not files:
        searched = ", ".join(str(r) for r in roots)
        raise RuntimeError(
            f"No generated runner YAMLs found under {searched}. "
            "Run `just generate-arc-runners <cluster>` first to render them."
        )

    first_path = files[0]
    first = _parse_generated_file(first_path)

    for other_path in files[1:]:
        other = _parse_generated_file(other_path)
        _warn_if_disagrees(first_path, first, other_path, other)

    return first


if __name__ == "__main__":
    # Tiny CLI entry for ad-hoc debugging.
    upstream = Path(__file__).resolve().parent.parent.parent
    overhead = load_runner_pod_overhead(upstream)
    print(overhead)
