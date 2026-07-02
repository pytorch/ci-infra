"""Shared scope constants and helpers for the node-size optimizer (Phase 1 catalog and Phase 2 search)."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts" / "python"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from analyze_node_utilization import HOOKS_OVERHEAD_CPU_M, HOOKS_OVERHEAD_MEM_MI  # noqa: E402
from build_csv import build_label_table  # noqa: E402

IN_SCOPE_FAMILIES = ("r7a", "c7i", "c7a", "m7i", "m8g", "m7g", "m6i", "r7i", "g5", "g6", "g4dn")
EXCLUDED_FAMILIES = ("p4d", "p5", "p6-b200")
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


def def_totals(defrow: dict) -> tuple[int, int, int]:
    """Pod-total (cpu_m, mem_mi, gpu) for a runner def including hooks tax.

    `build_label_table` gives vcpu (float) and memory_gib (float). Convert to
    the same millicore/MiB accounting the sim uses and add hook overhead so
    the resulting slot check matches what Karpenter schedules against.
    """
    cpu_m = round(defrow["vcpu"] * 1000) + HOOKS_OVERHEAD_CPU_M
    mem_mi = round(defrow["memory_gib"] * 1024) + HOOKS_OVERHEAD_MEM_MI
    gpu = int(defrow["gpu"])
    return cpu_m, mem_mi, gpu


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
