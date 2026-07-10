"""Single-source-of-truth loader for runner/hooks overhead used by sim and catalog.

Both the sim (`sim_load`) and the eligibility catalog (`optimize_config.def_totals`)
must add identical per-pod hooks overhead when translating a def's vcpu/memory
into scheduled pod requests. Any drift between the two produces false-positive
feasibility calls in the catalog that the sim then refuses to schedule.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts" / "python"))

from analyze_node_utilization import HOOKS_OVERHEAD_CPU_M as FALLBACK_HOOKS_CPU_M  # noqa: E402
from analyze_node_utilization import HOOKS_OVERHEAD_MEM_MI as FALLBACK_HOOKS_MEM_MI  # noqa: E402

FALLBACK_RUNNER_CPU_M = 750
FALLBACK_RUNNER_MEM_MI = 1024


def load_runner_overhead() -> tuple[int, int, int, int]:
    """Return (workflow_extra_cpu_m, workflow_extra_mem_mi, runner_cpu_m, runner_mem_mi).

    Reads the live rendered runner YAMLs via runner_overhead.load_runner_pod_overhead.
    Falls back to the module-level constants on any failure (missing files,
    unrendered generated/*.yaml, malformed YAML) so callers still get a usable
    number in dev environments that haven't run `just generate-arc-runners`.
    """
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
