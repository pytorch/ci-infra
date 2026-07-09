#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6.0"]
# ///
"""Phase 1 of the node-size optimizer: eligibility catalog.

For each (def, in-family instance) pair in scope, compute whether the pair is
feasible under the D4 tight-fit rule (see optimize.md) and, if so, what the
resulting adjusted pod slot and per-node pod count `N` are.

The catalog is the feasibility oracle every partition-level candidate config
consults in Phase 2. It runs in seconds, no sim invocation required.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts" / "python"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from analyze_node_utilization import compute_allocatable  # noqa: E402
from daemonset_overhead import DaemonSetOverhead, discover_daemonsets  # noqa: E402
from instance_specs import INSTANCE_SPECS  # noqa: E402
from optimize_config import (  # noqa: E402
    IN_SCOPE_FAMILIES,
    def_totals,
    load_defs_by_family,
)
from sim_nodes import _daemonsets_for_fleet  # noqa: E402

# D4 tight-fit tolerance constants (see optimize.md § D4).
# CPU bounds are on the MAIN pod's integer vCPU (the number the operator writes
# in the def YAML's `vcpu:` field). The runner-container-hooks sidecar's cpu
# request is fixed and rides on top of the main pod — it is never part of the
# adjustment axis.
MEM_LOWER_PCT = 0.90


@dataclass(frozen=True)
class EligibleEntry:
    """A feasible (def, instance) pair with its D4 tight-fit slot and pod count."""

    def_label: str
    instance: str
    n: int
    slot_cpu_m: int
    slot_mem_mi: int
    slot_gpu: int
    orig_cpu_m: int
    orig_mem_mi: int
    orig_gpu: int
    new_main_vcpu: int
    orig_main_vcpu: int
    new_main_memory_gib: int
    orig_main_memory_gib: int
    adj_cpu_pct: float
    adj_mem_pct: float


def instances_for_family(family: str) -> list[str]:
    """All INSTANCE_SPECS entries whose prefix matches `<family>.`, excluding metal variants.

    Metal instances are whole-node-per-pod by design (see optimize.md D8) and
    are out of scope for the tight-fit optimizer — filtering them here keeps
    the catalog from recommending an instance the search must never pick.
    """
    prefix = f"{family}."
    return sorted(k for k in INSTANCE_SPECS if k.startswith(prefix) and ".metal" not in k)


def _floor_main_vcpu(orig_main_vcpu: int) -> int:
    """Lowest legal main vCPU: 1 vCPU or 10% down, whichever is more generous,
    never below 1. Single source for the CPU down-floor used both to pack pods
    (N computed as if every pod shrinks to this floor) and to gate the filled-
    back slot in `_main_vcpu_bounds`.
    """
    return max(1, min(orig_main_vcpu - 1, math.ceil(orig_main_vcpu * 0.90)))


def _main_vcpu_bounds(orig_main_vcpu: int) -> tuple[int, int]:
    """Return (lo, hi) inclusive integer bounds for a legal `new_main_vcpu`.

    Lower: 1 vCPU or 10% down, whichever is more generous (so tiny defs like
    vcpu=2 keep a real range).
    Upper: `max(orig + 1, ceil(orig * 1.35))` — at least 1 vCPU or 35% up.
    """
    lo = _floor_main_vcpu(orig_main_vcpu)
    hi = max(orig_main_vcpu + 1, math.ceil(orig_main_vcpu * 1.35))
    return lo, hi


def _within_main_vcpu_bounds(new_main_vcpu: int, orig_main_vcpu: int) -> bool:
    lo, hi = _main_vcpu_bounds(orig_main_vcpu)
    return lo <= new_main_vcpu <= hi


def _within_mem_bounds(slot_mem_mi: int, orig_mem_mi: int) -> bool:
    return slot_mem_mi >= int(orig_mem_mi * MEM_LOWER_PCT)


def eligibility_for_pair(
    def_label: str,
    orig_cpu_m: int,
    orig_mem_mi: int,
    orig_gpu: int,
    orig_main_vcpu: int,
    orig_main_memory_gib: int,
    instance: str,
    scoped_daemonsets: list[DaemonSetOverhead],
) -> EligibleEntry | None:
    """Compute the tight-fit outcome for one (def, instance) pair.

    Returns an EligibleEntry when feasible under D4, or None when the pair
    cannot fit at all (N=0) or falls outside D4 tolerances on any dimension.
    """
    alloc = compute_allocatable(instance, scoped_daemonsets)
    if alloc is None:
        return None
    alloc_cpu_m = alloc["allocatable_cpu_m"]
    alloc_mem_mi = alloc["allocatable_mem_mi"]
    alloc_gpu = alloc["allocatable_gpu"]

    # A CPU/memory-only def cannot land on a GPU node (the pod would occupy a
    # GPU-attached instance without a nvidia.com/gpu request), and a GPU def
    # obviously needs a GPU instance. Filter both directions before any math.
    if orig_gpu > 0 and alloc_gpu == 0:
        return None
    if orig_gpu == 0 and alloc_gpu > 0:
        return None

    if orig_cpu_m <= 0 or orig_mem_mi <= 0 or orig_main_vcpu <= 0 or orig_main_memory_gib <= 0:
        return None

    # Sidecar CPU/memory is the fixed portion of orig_cpu_m / orig_mem_mi that
    # is NOT the main pod. main_vcpu and main_memory_gib are the operator-tunable
    # knobs; the sidecar rides on top at fixed request sizes regardless of main
    # pod size.
    sidecar_cpu_m = orig_cpu_m - orig_main_vcpu * 1000
    sidecar_mem_mi = orig_mem_mi - orig_main_memory_gib * 1024
    if sidecar_cpu_m < 0 or sidecar_mem_mi < 0:
        return None

    # Pack pods as if every main pod shrinks to its down-floor: N is computed
    # against the smallest legal slot so a pod may shrink just enough to cross
    # an integer packing boundary (fit one MORE per node). The slot is filled
    # back up afterward, capped at the operator's original size.
    floor_main_vcpu = _floor_main_vcpu(orig_main_vcpu)
    floor_cpu_m = floor_main_vcpu * 1000 + sidecar_cpu_m
    floor_mem_mi = int(orig_mem_mi * MEM_LOWER_PCT)
    n_cpu = alloc_cpu_m // floor_cpu_m
    n_mem = alloc_mem_mi // floor_mem_mi
    n_gpu = (alloc_gpu // orig_gpu) if orig_gpu > 0 else n_cpu
    n = min(n_cpu, n_mem, n_gpu)
    if n <= 0:
        return None

    raw_available_per_pod_cpu_m = alloc_cpu_m // n
    # Subtract the fixed sidecar first, THEN floor to whole vCPU — otherwise
    # the sidecar's 320m gets lumped into the "rounded to whole vCPU" number
    # and the resulting slot cannot actually deploy (main_vcpu * 1000 + 320m
    # would exceed the slot allowance).
    available_main_cpu_m = raw_available_per_pod_cpu_m - sidecar_cpu_m
    new_main_vcpu = max(1, available_main_cpu_m // 1000)
    # Never hand back more than the operator set: the fill-back only returns
    # headroom that the shrink freed up, it does not grow the pod.
    new_main_vcpu = min(new_main_vcpu, orig_main_vcpu)
    slot_cpu_m = new_main_vcpu * 1000 + sidecar_cpu_m
    # Memory: same shape as CPU. Subtract the sidecar first, then floor to
    # whole GiB so the reported main_memory_gib matches what the operator
    # writes back into the def YAML. Slot_mem_mi is rebuilt from the whole-GiB
    # main plus the fixed sidecar so the sim sees the exact deployable total.
    raw_available_per_pod_mem_mi = alloc_mem_mi // n
    available_main_mem_mi = raw_available_per_pod_mem_mi - sidecar_mem_mi
    new_main_memory_gib = max(1, available_main_mem_mi // 1024)
    new_main_memory_gib = min(new_main_memory_gib, orig_main_memory_gib)
    slot_mem_mi = new_main_memory_gib * 1024 + sidecar_mem_mi
    slot_gpu = (alloc_gpu // n) if orig_gpu > 0 else 0

    if not _within_main_vcpu_bounds(new_main_vcpu, orig_main_vcpu):
        return None
    if not _within_mem_bounds(slot_mem_mi, orig_mem_mi):
        return None
    # GPU count per pod is fixed — the tight-fit rule may not silently change it.
    if orig_gpu > 0 and slot_gpu != orig_gpu:
        return None

    adj_cpu_pct = 100.0 * (new_main_vcpu - orig_main_vcpu) / orig_main_vcpu
    adj_mem_pct = 100.0 * (new_main_memory_gib - orig_main_memory_gib) / orig_main_memory_gib

    return EligibleEntry(
        def_label=def_label,
        instance=instance,
        n=int(n),
        slot_cpu_m=int(slot_cpu_m),
        slot_mem_mi=int(slot_mem_mi),
        slot_gpu=int(slot_gpu),
        orig_cpu_m=int(orig_cpu_m),
        orig_mem_mi=int(orig_mem_mi),
        orig_gpu=int(orig_gpu),
        new_main_vcpu=int(new_main_vcpu),
        orig_main_vcpu=int(orig_main_vcpu),
        new_main_memory_gib=int(new_main_memory_gib),
        orig_main_memory_gib=int(orig_main_memory_gib),
        adj_cpu_pct=adj_cpu_pct,
        adj_mem_pct=adj_mem_pct,
    )


def build_eligibility_catalog(
    families: Iterable[str] | None = None,
    daemonsets: list[DaemonSetOverhead] | None = None,
) -> dict[str, list[EligibleEntry]]:
    """Return `{family: [EligibleEntry, ...]}` covering every feasible pair.

    A def with N eligible instances contributes N entries. A def with zero
    eligible entries in its family means no in-family instance can host it
    under tight-fit bounds — that def is infeasible; a warning is logged and
    the def contributes nothing to the catalog.
    """
    fams = tuple(families) if families is not None else IN_SCOPE_FAMILIES
    if daemonsets is None:
        daemonsets = discover_daemonsets(REPO_ROOT)
    defs_by_family = load_defs_by_family()

    catalog: dict[str, list[EligibleEntry]] = {}
    for family in fams:
        defs = defs_by_family.get(family, [])
        if not defs:
            print(f"WARN: no in-scope defs found for family {family!r}", file=sys.stderr)
            catalog[family] = []
            continue
        entries: list[EligibleEntry] = []
        instances = instances_for_family(family)
        # Fleet-scoped DaemonSets today are all keyed on `c7i-runner` (see
        # FLEET_SCOPED_DAEMONSETS in sim_nodes.py). Since c7i-runner is
        # out-of-scope for the optimizer (EXCLUDED_FLEETS in optimize_config.py),
        # passing the family name here is equivalent to passing any sub-nodepool
        # name for in-scope families. If new fleet-scoped DSes are added keyed
        # on other fleets, this call needs to be lifted per sub-nodepool.
        scoped_ds = _daemonsets_for_fleet(daemonsets, family)
        for d in defs:
            orig_cpu_m, orig_mem_mi, orig_gpu, orig_main_vcpu, orig_main_memory_gib = def_totals(d)
            per_def_count = 0
            for inst in instances:
                entry = eligibility_for_pair(
                    d["name"],
                    orig_cpu_m,
                    orig_mem_mi,
                    orig_gpu,
                    orig_main_vcpu,
                    orig_main_memory_gib,
                    inst,
                    scoped_ds,
                )
                if entry is not None:
                    entries.append(entry)
                    per_def_count += 1
            if per_def_count == 0:
                print(
                    f"WARN: def {d['name']!r} in family {family!r} has zero eligible in-family instances",
                    file=sys.stderr,
                )
        entries.sort(key=lambda e: (e.def_label, e.instance))
        catalog[family] = entries
    return catalog


def _fmt_slot_cpu(slot_cpu_m: int) -> str:
    return f"{slot_cpu_m / 1000:.1f}c"


def _fmt_slot_mem(slot_mem_mi: int) -> str:
    return f"{slot_mem_mi / 1024:.1f}Gi"


def _fmt_pct(x: float) -> str:
    return f"{x:+.1f}%"


def _defs_index(defs_by_family: dict[str, list[dict]]) -> dict[str, dict]:
    """Flat {def_name: defrow} across all families for orig-request lookup."""
    out: dict[str, dict] = {}
    for defs in defs_by_family.values():
        for d in defs:
            out[d["name"]] = d
    return out


def print_family_report(
    family: str,
    entries: list[EligibleEntry],
    defs: list[dict],
    verbose: bool,
) -> None:
    print(f"==== Eligibility catalog: family={family} ====")
    hdr = f"  {'def':<32} {'instance':<20} {'N':>3}  {'slot_cpu':>8} {'slot_mem':>9} {'adj_cpu':>8} {'adj_mem':>8}"
    print(hdr)
    by_def: dict[str, list[EligibleEntry]] = {}
    for e in entries:
        by_def.setdefault(e.def_label, []).append(e)

    for d in defs:
        name = d["name"]
        elig = by_def.get(name, [])
        if not elig:
            orig_cpu_m, orig_mem_mi, _, _, _ = def_totals(d)
            print(
                f"  {name:<32} {'(no eligible in-family instance)':<20}     "
                f"orig={_fmt_slot_cpu(orig_cpu_m)}/{_fmt_slot_mem(orig_mem_mi)}"
            )
            continue
        for e in elig:
            print(
                f"  {e.def_label:<32} {e.instance:<20} {e.n:>3}  "
                f"main vcpu {e.orig_main_vcpu}->{e.new_main_vcpu} "
                f"main mem {e.orig_main_memory_gib}->{e.new_main_memory_gib} Gi "
                f"{_fmt_pct(e.adj_cpu_pct):>8} {_fmt_pct(e.adj_mem_pct):>8}"
            )
    print()

    if verbose:
        print(f"  Per-def eligible counts (family={family}):")
        for d in defs:
            elig = by_def.get(d["name"], [])
            insts = ", ".join(e.instance for e in elig) if elig else "(none)"
            print(f"    {d['name']:<32} {len(elig):>3}  [{insts}]")
        print()


def print_global_summary(
    catalog: dict[str, list[EligibleEntry]],
    defs_by_family: dict[str, list[dict]],
) -> None:
    print("==== Per-family summary ====")
    print(f"  {'family':<8} {'defs':>6} {'eligible_defs':>14} {'pairs':>7} {'infeasible_defs':>16}")
    for family, entries in catalog.items():
        defs = defs_by_family.get(family, [])
        eligible_names = {e.def_label for e in entries}
        eligible_defs = len(eligible_names)
        infeasible_defs = len(defs) - eligible_defs
        print(f"  {family:<8} {len(defs):>6} {eligible_defs:>14} {len(entries):>7} {infeasible_defs:>16}")
    print()

    infeasible: list[tuple[str, str]] = []
    for family, entries in catalog.items():
        defs = defs_by_family.get(family, [])
        eligible_names = {e.def_label for e in entries}
        for d in defs:
            if d["name"] not in eligible_names:
                infeasible.append((family, d["name"]))
    print("==== Defs with ZERO eligible in-family instances (bug indicator) ====")
    if not infeasible:
        print("  (none)")
    else:
        for family, name in infeasible:
            print(f"  {family}: {name}")
    print()


def to_json(
    catalog: dict[str, list[EligibleEntry]],
    defs_by_family: dict[str, list[dict]],
) -> dict:
    """Deterministically-sorted JSON payload for Phase B consumption."""
    families_out: dict[str, dict] = {}
    for family in sorted(catalog):
        entries = catalog[family]
        by_def: dict[str, list[EligibleEntry]] = {}
        for e in entries:
            by_def.setdefault(e.def_label, []).append(e)

        defs_out: dict[str, dict] = {}
        for d in defs_by_family.get(family, []):
            orig_cpu_m, orig_mem_mi, orig_gpu, orig_main_vcpu, orig_main_memory_gib = def_totals(d)
            elig = sorted(by_def.get(d["name"], []), key=lambda e: e.instance)
            defs_out[d["name"]] = {
                "orig_cpu_m": orig_cpu_m,
                "orig_mem_mi": orig_mem_mi,
                "orig_gpu": orig_gpu,
                "orig_main_vcpu": orig_main_vcpu,
                "orig_main_memory_gib": orig_main_memory_gib,
                "eligible": [
                    {
                        "instance": e.instance,
                        "n": e.n,
                        "slot_cpu_m": e.slot_cpu_m,
                        "slot_mem_mi": e.slot_mem_mi,
                        "slot_gpu": e.slot_gpu,
                        "new_main_vcpu": e.new_main_vcpu,
                        "new_main_memory_gib": e.new_main_memory_gib,
                        "adj_cpu_pct": round(e.adj_cpu_pct, 4),
                        "adj_mem_pct": round(e.adj_mem_pct, 4),
                    }
                    for e in elig
                ],
            }
        families_out[family] = {"defs": defs_out}
    return {"families": families_out}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--family",
        action="append",
        help="Restrict to one or more in-scope families (repeatable)",
    )
    ap.add_argument("--output", default=None, help="Optional JSON output path")
    ap.add_argument(
        "--verbose",
        action="store_true",
        help="Print the per-def eligible-instances summary alongside the main table",
    )
    args = ap.parse_args(argv)

    families = tuple(args.family) if args.family else IN_SCOPE_FAMILIES
    for f in families:
        if f not in IN_SCOPE_FAMILIES:
            print(
                f"ERROR: {f!r} is not an in-scope family. In scope: {IN_SCOPE_FAMILIES}",
                file=sys.stderr,
            )
            return 2

    daemonsets = discover_daemonsets(REPO_ROOT)
    catalog = build_eligibility_catalog(families=families, daemonsets=daemonsets)
    defs_by_family = load_defs_by_family()

    for family in families:
        print_family_report(family, catalog.get(family, []), defs_by_family.get(family, []), args.verbose)
    print_global_summary(catalog, {f: defs_by_family.get(f, []) for f in families})

    if args.output:
        out_path = Path(args.output).resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        payload = to_json(catalog, defs_by_family)
        out_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        print(f"wrote {out_path}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
