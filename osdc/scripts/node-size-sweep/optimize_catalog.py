#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6.0"]
# ///
"""Phase 1 of the node-size optimizer: shape catalog + per-def eligibility.

Analytical. No sim runs. For each in-scope fleet family:

  1. Enumerate every AWS instance size present in INSTANCE_SPECS for that
     family and compute allocatable (kubelet-reserved + DaemonSet overhead).
  2. For every (instance, N) split, emit a shape (slot_cpu, slot_mem,
     slot_gpu, overhead_frac) that respects the D4 enumeration rules.
  3. For every runner def belonging to the family, filter to eligible shapes
     (slot >= request on all 3 dims), pick the best per def by CPU efficiency,
     and record waste per dim.
  4. Compute the per-fleet theoretical util ceiling using the ranking metric
     opt_max = max(req_cpu / (alloc_cpu + ds_cpu), req_mem / (alloc_mem +
     ds_mem)) averaged uniformly across defs (Phase 1 caveat: no
     per-def load weights yet).

Runs in seconds. Output: three human-readable tables on stdout plus a JSON
dump for downstream phases.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts" / "python"))

from analyze_node_utilization import (  # noqa: E402
    HOOKS_OVERHEAD_CPU_M,
    HOOKS_OVERHEAD_MEM_MI,
    compute_allocatable,
)
from daemonset_overhead import discover_daemonsets  # noqa: E402
from instance_specs import INSTANCE_SPECS  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_csv import build_label_table  # noqa: E402
from sim_nodes import _daemonsets_for_fleet  # noqa: E402

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


def instances_for_family(family: str) -> list[str]:
    """Return all INSTANCE_SPECS entries whose prefix matches `family.`."""
    prefix = f"{family}."
    return sorted(k for k in INSTANCE_SPECS if k.startswith(prefix))


def _def_totals(defrow: dict) -> tuple[int, int, int]:
    """Pod-total (cpu_m, mem_mi, gpu) for a runner def including hooks tax.

    `build_label_table` gives vcpu (float) and memory_gib (float). Convert to
    the same millicore/MiB accounting the sim uses and add hook overhead so
    the resulting slot check matches what Karpenter schedules against.
    """
    cpu_m = round(defrow["vcpu"] * 1000) + HOOKS_OVERHEAD_CPU_M
    mem_mi = round(defrow["memory_gib"] * 1024) + HOOKS_OVERHEAD_MEM_MI
    gpu = int(defrow["gpu"])
    return cpu_m, mem_mi, gpu


def generate_catalog(family: str, defs: list[dict], daemonsets) -> list[dict]:
    """For every instance in a family, yield every valid N split as a shape.

    N_max per instance is the largest split N such that AT LEAST ONE def in
    the family still fits a slot at that N (i.e. `max_over_defs(min(alloc_cpu
    // req_cpu, alloc_mem // req_mem, alloc_gpu // req_gpu))`). Enumerating
    beyond that produces slots too small for any real workload and
    explodes the catalog by orders of magnitude.
    """
    out: list[dict] = []
    if not defs:
        return out
    reqs = [_def_totals(d) for d in defs]
    scoped_ds = _daemonsets_for_fleet(daemonsets, family)
    for inst in instances_for_family(family):
        alloc = compute_allocatable(inst, scoped_ds)
        if alloc is None:
            continue
        spec = INSTANCE_SPECS[inst]
        total_cpu_m = spec["vcpu"] * 1000
        alloc_cpu_m = alloc["allocatable_cpu_m"]
        alloc_mem_mi = alloc["allocatable_mem_mi"]
        alloc_gpu = alloc["allocatable_gpu"]
        overhead_frac = 1 - (alloc_cpu_m / total_cpu_m) if total_cpu_m > 0 else 0.0

        n_max = 0
        for req_cpu, req_mem, req_gpu in reqs:
            if req_gpu > 0 and alloc_gpu == 0:
                continue
            if req_gpu == 0 and alloc_gpu > 0:
                continue
            per_def = min(alloc_cpu_m // req_cpu, alloc_mem_mi // req_mem)
            if req_gpu > 0:
                per_def = min(per_def, alloc_gpu // req_gpu)
            n_max = max(n_max, per_def)
        if alloc_gpu > 0:
            n_max = min(n_max, alloc_gpu)
        if n_max <= 0:
            continue
        for n in range(1, int(n_max) + 1):
            if alloc_gpu > 0 and alloc_gpu % n != 0:
                continue
            slot_cpu = alloc_cpu_m // n
            slot_mem = alloc_mem_mi // n
            slot_gpu = alloc_gpu // n if alloc_gpu > 0 else 0
            if slot_cpu <= 0 or slot_mem <= 0:
                continue
            if alloc_gpu > 0 and slot_gpu == 0:
                continue
            out.append(
                {
                    "instance": inst,
                    "N": n,
                    "slot_cpu_m": slot_cpu,
                    "slot_mem_mi": slot_mem,
                    "slot_gpu": slot_gpu,
                    "alloc_cpu_m": alloc_cpu_m,
                    "alloc_mem_mi": alloc_mem_mi,
                    "alloc_gpu": alloc_gpu,
                    "ds_cpu_m": alloc["ds_cpu_m"],
                    "ds_mem_mi": alloc["ds_mem_mi"],
                    "overhead_frac": overhead_frac,
                }
            )
    return out


def eligible_shapes(req_cpu_m: int, req_mem_mi: int, req_gpu: int, catalog: list[dict]) -> list[dict]:
    """Filter catalog to shapes that fit request on ALL 3 dims."""
    return [
        s
        for s in catalog
        if s["slot_cpu_m"] >= req_cpu_m and s["slot_mem_mi"] >= req_mem_mi and s["slot_gpu"] >= req_gpu
    ]


def score_shape(req_cpu_m: int, req_mem_mi: int, shape: dict) -> dict:
    """Per-pod fit metrics used for ranking and eyeball comparison."""
    cpu_eff = req_cpu_m / shape["slot_cpu_m"]
    mem_eff = req_mem_mi / shape["slot_mem_mi"]
    return {
        "cpu_eff": cpu_eff,
        "mem_eff": mem_eff,
        "binding_eff": max(cpu_eff, mem_eff),
        "waste_cpu_m": shape["slot_cpu_m"] - req_cpu_m,
        "waste_mem_mi": shape["slot_mem_mi"] - req_mem_mi,
    }


def _load_defs_by_family() -> dict[str, list[dict]]:
    """Bucket the runner def table by instance-family prefix."""
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


def analyse_family(family: str, defs: list[dict], daemonsets) -> dict:
    """Build catalog, per-def eligibility, and theoretical ceiling for a family."""
    catalog = generate_catalog(family, defs, daemonsets)
    eligibility: dict[str, dict] = {}
    unfittable: list[str] = []
    best_contribs: list[float] = []
    for d in defs:
        req_cpu, req_mem, req_gpu = _def_totals(d)
        elig = eligible_shapes(req_cpu, req_mem, req_gpu, catalog)
        if not elig:
            unfittable.append(d["name"])
            eligibility[d["name"]] = {
                "req_cpu_m": req_cpu,
                "req_mem_mi": req_mem,
                "req_gpu": req_gpu,
                "shapes": [],
                "best": None,
            }
            continue
        scored = []
        for s in elig:
            m = score_shape(req_cpu, req_mem, s)
            row = {**s, **m}
            row["opt_share_cpu"] = req_cpu / (s["alloc_cpu_m"] + s["ds_cpu_m"])
            row["opt_share_mem"] = req_mem / (s["alloc_mem_mi"] + s["ds_mem_mi"])
            row["opt_share"] = max(row["opt_share_cpu"], row["opt_share_mem"]) * s["N"]
            scored.append(row)
        best = max(scored, key=lambda r: r["binding_eff"])
        eligibility[d["name"]] = {
            "req_cpu_m": req_cpu,
            "req_mem_mi": req_mem,
            "req_gpu": req_gpu,
            "shapes": scored,
            "best": best,
        }
        best_contribs.append(best["opt_share"])
    ceiling = sum(best_contribs) / len(best_contribs) if best_contribs else 0.0
    return {
        "catalog": catalog,
        "eligibility": eligibility,
        "unfittable": unfittable,
        "ceiling": ceiling,
    }


def _fmt_pct(x: float) -> str:
    return f"{x * 100:5.1f}%"


def print_catalog_table(family: str, catalog: list[dict]) -> None:
    print(f"==== Shape catalog: family={family} ====")
    print(
        f"  {'instance':<22} {'alloc_cpu_m':>12} {'alloc_mem_mi':>13} {'alloc_gpu':>10} {'overhead':>9} {'N-values':>14}"
    )
    by_inst: dict[str, list[int]] = {}
    for s in catalog:
        by_inst.setdefault(s["instance"], []).append(s["N"])
    for inst in sorted(by_inst):
        ns = sorted(by_inst[inst])
        alloc = next(s for s in catalog if s["instance"] == inst)
        n_range = f"{min(ns)}..{max(ns)}" if len(ns) > 1 else str(ns[0])
        print(
            f"  {inst:<22} {alloc['alloc_cpu_m']:>12} {alloc['alloc_mem_mi']:>13} "
            f"{alloc['alloc_gpu']:>10} {_fmt_pct(alloc['overhead_frac']):>9} {n_range:>14}"
        )
    print()


def print_eligibility_table(family: str, defs: list[dict], eligibility: dict, verbose: bool) -> None:
    print(f"==== Per-def eligibility: family={family} ====")
    print(f"  {'def':<32} {'req_cpu':>7} {'req_mem':>8} {'best_shape':<38} {'cpu':>6} {'mem':>6} {'bind':>6}")
    for d in defs:
        name = d["name"]
        entry = eligibility[name]
        if entry["best"] is None:
            print(f"  {name:<32} {entry['req_cpu_m']:>7} {entry['req_mem_mi']:>8} {'NO ELIGIBLE SHAPE':<38}")
            continue
        best = entry["best"]
        shape_str = (
            f"{best['instance']} N={best['N']} slot={best['slot_cpu_m'] // 1000}c/{best['slot_mem_mi'] // 1024}Gi"
        )
        print(
            f"  {name:<32} {entry['req_cpu_m']:>7} {entry['req_mem_mi']:>8} {shape_str:<38} "
            f"{_fmt_pct(best['cpu_eff']):>6} {_fmt_pct(best['mem_eff']):>6} {_fmt_pct(best['binding_eff']):>6}"
        )
        if verbose:
            for s in sorted(entry["shapes"], key=lambda r: -r["binding_eff"]):
                if s is best:
                    continue
                sstr = f"{s['instance']} N={s['N']} slot={s['slot_cpu_m'] // 1000}c/{s['slot_mem_mi'] // 1024}Gi"
                print(
                    f"  {'':<32} {'':>7} {'':>8} {sstr:<38} "
                    f"{_fmt_pct(s['cpu_eff']):>6} {_fmt_pct(s['mem_eff']):>6} {_fmt_pct(s['binding_eff']):>6}"
                )
    print()


def print_ceiling_table(results: dict[str, dict]) -> None:
    print("==== Per-fleet theoretical ceiling (opt metric, uniform def weighting) ====")
    print(f"  {'fleet':<8} {'ceiling(opt_max)':>18}")
    for family in IN_SCOPE_FAMILIES:
        r = results.get(family)
        if r is None:
            continue
        print(f"  {family:<8} {_fmt_pct(r['ceiling']):>18}")
    print()


def to_json(results: dict[str, dict]) -> dict:
    """Serialize to JSON schema documented in optimize.md."""
    catalog_out: dict[str, list[dict]] = {}
    elig_out: dict[str, list[dict]] = {}
    ceiling_out: dict[str, float] = {}
    for family, r in results.items():
        catalog_out[family] = [
            {
                "instance": s["instance"],
                "N": s["N"],
                "slot_cpu_m": s["slot_cpu_m"],
                "slot_mem_mi": s["slot_mem_mi"],
                "slot_gpu": s["slot_gpu"],
                "alloc_cpu_m": s["alloc_cpu_m"],
                "alloc_mem_mi": s["alloc_mem_mi"],
                "alloc_gpu": s["alloc_gpu"],
                "ds_cpu_m": s["ds_cpu_m"],
                "ds_mem_mi": s["ds_mem_mi"],
                "overhead_frac": round(s["overhead_frac"], 6),
            }
            for s in r["catalog"]
        ]
        for defname, entry in r["eligibility"].items():
            elig_out[defname] = [
                {
                    "instance": s["instance"],
                    "N": s["N"],
                    "slot_cpu_m": s["slot_cpu_m"],
                    "slot_mem_mi": s["slot_mem_mi"],
                    "slot_gpu": s["slot_gpu"],
                    "waste_cpu_m": s["waste_cpu_m"],
                    "waste_mem_mi": s["waste_mem_mi"],
                    "efficiency": {
                        "cpu": round(s["cpu_eff"], 6),
                        "mem": round(s["mem_eff"], 6),
                        "binding": round(s["binding_eff"], 6),
                    },
                    "opt_share": round(s["opt_share"], 6),
                }
                for s in entry["shapes"]
            ]
        ceiling_out[family] = round(r["ceiling"], 6)
    return {"catalog": catalog_out, "eligibility": elig_out, "ceiling": ceiling_out}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--family", action="append", help="Restrict to one or more families (repeatable)")
    ap.add_argument("--output", default="optimize_catalog.json", help="JSON output path")
    ap.add_argument("--verbose", action="store_true", help="Show all eligible shapes per def, not just the best")
    args = ap.parse_args(argv)

    families = tuple(args.family) if args.family else IN_SCOPE_FAMILIES
    for f in families:
        if f not in IN_SCOPE_FAMILIES:
            print(f"ERROR: {f!r} is not an in-scope family. In scope: {IN_SCOPE_FAMILIES}", file=sys.stderr)
            return 2

    upstream_dir = REPO_ROOT
    daemonsets = discover_daemonsets(upstream_dir)
    defs_by_family = _load_defs_by_family()

    results: dict[str, dict] = {}
    unfittable_global: dict[str, list[str]] = {}
    for family in families:
        defs = defs_by_family.get(family, [])
        if not defs:
            print(f"WARN: no in-scope defs found for family {family!r}", file=sys.stderr)
            continue
        r = analyse_family(family, defs, daemonsets)
        results[family] = r
        if r["unfittable"]:
            unfittable_global[family] = r["unfittable"]

    for family in families:
        if family not in results:
            continue
        print_catalog_table(family, results[family]["catalog"])
        print_eligibility_table(family, defs_by_family[family], results[family]["eligibility"], args.verbose)

    print_ceiling_table(results)

    if unfittable_global:
        print("==== Defs with ZERO eligible shapes (bug indicator) ====", file=sys.stderr)
        for family, names in unfittable_global.items():
            for n in names:
                print(f"  {family}: {n}", file=sys.stderr)
        print(file=sys.stderr)

    out_path = Path(args.output).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(to_json(results), indent=2, sort_keys=True) + "\n")
    print(f"wrote {out_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
