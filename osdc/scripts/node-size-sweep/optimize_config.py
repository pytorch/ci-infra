"""Shared scope constants and helpers for the node-size optimizer (Phase 1 catalog and Phase 2 search)."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts" / "python"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from build_csv import build_label_table  # noqa: E402
from runner_hooks import load_runner_overhead  # noqa: E402

IN_SCOPE_FAMILIES = ("r7a", "c7i", "c7a", "m7i", "m8g", "m7g", "m6i", "r7i")
EXCLUDED_FAMILIES = ("p4d", "p5", "p6-b200", "g5", "g6", "g4dn")
EXCLUDED_FLEETS = frozenset(
    {
        "p4d-large",
        "p5-large",
        "p6-b200-large",
        "c7a-large",
        "c7i-large",
        "c7i-runner",
        "g4dn-metal",
        "g5-large",
        "m7g-metal",
        "m8g-large",
        "r7a-large",
    }
)

# Prod-parity sim flags — single source of truth for both benchmark's calibration
# run and optimize_search's cache key. Callers merge with their own overrides.
PROD_PARITY_SIM_FLAGS: dict[str, object] = {
    "daemonsets_in_metric": True,
    "phantom_pods_enabled": True,
    "empty_ttl_buckets": 1,
}


def def_totals(defrow: dict) -> tuple[int, int, int, int, int]:
    """Pod-total (cpu_m, mem_mi, gpu, main_vcpu, main_memory_gib) for a runner def including hooks tax.

    `build_label_table` gives vcpu (float) and memory_gib (float). Convert to
    the same millicore/MiB accounting the sim uses (int truncation, matches
    sim_load.load_jobs exactly) and add hook overhead read from the same
    shared source so the resulting slot check matches what Karpenter
    schedules against.

    `main_vcpu` is the whole-integer vcpu the operator sets in the def YAML
    (2, 8, 16, 46, ...). `main_memory_gib` is the whole-integer GiB the
    operator sets in the def YAML's `memory:` field. Both are operator-tunable
    knobs; the sidecar (`hooks_cpu` / `hooks_mem`) is fixed and rides on top.
    """
    hooks_cpu, hooks_mem, _, _ = load_runner_overhead()
    main_vcpu = int(defrow["vcpu"])
    main_memory_gib = int(defrow["memory_gib"])
    cpu_m = main_vcpu * 1000 + hooks_cpu
    mem_mi = int(defrow["memory_gib"] * 1024) + hooks_mem
    gpu = int(defrow["gpu"])
    return cpu_m, mem_mi, gpu, main_vcpu, main_memory_gib


def load_defs_by_family() -> dict[str, list[dict]]:
    """Bucket the runner def table by instance-family prefix, filtered by scope constants."""
    table = build_label_table()
    by_family: dict[str, list[dict]] = {f: [] for f in IN_SCOPE_FAMILIES}
    for name, row in table.items():
        inst = row["instance_type"]
        family = inst.split(".")[0]
        if family in EXCLUDED_FAMILIES:
            continue
        if row.get("nodepool") in EXCLUDED_FLEETS:
            continue
        if family not in by_family:
            continue
        row = dict(row)
        row["name"] = name
        by_family[family].append(row)
    return by_family
