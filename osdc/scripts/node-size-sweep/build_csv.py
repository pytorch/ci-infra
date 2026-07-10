#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6.0"]
# ///
"""One-off: build label -> (nodepool, nodepool_fraction) table + join to HUD jobs.

Hacky tool for the node-size sweep analysis. Splits into two subcommands:

    build-table   Print label -> (nodepool, nodepool_fraction) mapping as JSON.
    extract       Read one or more raw ClickHouse JSON chunks (each is a JSON
                  list of [label, started_at, completed_at, runtime_s] rows,
                  which is what the ClickHouse MCP `run_clickhouse_query`
                  drops into `result_file`), filter, bucket, join, and write
                  a CSV.

Columns in the CSV:
    provider, label, nodepool, nodepool_fraction, start_time, end_time

`provider` is either 'mt' or 'lf' — extracted from the HUD label prefix,
so the simulator can filter (e.g. drop 'lf' to simulate a single Meta cluster).

`nodepool_fraction` is 1/N notation stored as the N — e.g. a 1/12 slice of a
c7a.24xlarge is stored as 12. `1` means "the runner takes the whole node".
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import re
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts" / "python"))

from fleet_naming import derive_fleet_name  # noqa: E402
from instance_specs import INSTANCE_SPECS  # noqa: E402
from pytorch_workload_data import OLD_TO_NEW_LABEL  # noqa: E402

RUNNER_DEF_DIRS = [
    REPO_ROOT / "modules" / "arc-runners" / "defs",
    REPO_ROOT / "modules" / "arc-runners-h100" / "defs",
    REPO_ROOT / "modules" / "arc-runners-b200" / "defs",
]

# Runtime bucketing and filtering.
BUCKET_SECONDS = 300  # 5-min buckets on start_time
MAX_RUNTIME_S = 6 * 60 * 60  # drop >6h "bogus"
MIN_RUNTIME_S = 1


def _parse_mem_gib(mem: str) -> float:
    """Parse a k8s memory string ("225Gi", "1000Gi", "512Mi") into GiB (float)."""
    m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*([GMKgmk]i?)\s*$", mem)
    if not m:
        raise ValueError(f"unparseable memory: {mem!r}")
    val = float(m.group(1))
    unit = m.group(2).lower()
    if unit in ("gi", "g"):
        return val
    if unit in ("mi", "m"):
        return val / 1024
    if unit in ("ki", "k"):
        return val / (1024 * 1024)
    raise ValueError(f"unknown unit in {mem!r}")


def _load_runner_defs() -> list[dict]:
    """Read every runner def and return a list of parsed runner dicts."""
    out = []
    for d in RUNNER_DEF_DIRS:
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.yaml")):
            data = yaml.safe_load(f.read_text()) or {}
            r = data.get("runner")
            if not isinstance(r, dict):
                continue
            out.append(r)
    return out


def _nodepool_capacity(instance_type: str) -> tuple[float, float]:
    """Return (vcpu, memory_gib) for the fleet's biggest instance.

    We index the fraction against the LARGEST instance in the fleet — that's
    the size ARC/Karpenter tends to prefer (weight=100 in every fleet def).
    This makes fraction comparisons cross-fleet consistent.
    """
    spec = INSTANCE_SPECS.get(instance_type)
    if spec is None:
        raise KeyError(f"instance_type {instance_type!r} missing from INSTANCE_SPECS")
    # memory_mi is real k8s capacity; convert to GiB for comparison with def mem.
    return float(spec["vcpu"]), spec["memory_mi"] / 1024


def _pick_fraction(runner_vcpu: float, runner_mem_gib: float, node_vcpu: float, node_mem_gib: float) -> int:
    """Return the N (as in 1/N) that best fits this runner on this node.

    N = min(fits_by_cpu, fits_by_mem), integer floor. Overhead intentionally
    ignored — this is a theoretical-maximum count, the simulator will layer
    overhead in later if it wants to.
    """
    if runner_vcpu <= 0 or runner_mem_gib <= 0:
        return 1
    fits_cpu = int(node_vcpu // runner_vcpu)
    fits_mem = int(node_mem_gib // runner_mem_gib)
    return max(1, min(fits_cpu, fits_mem))


def build_label_table() -> dict[str, dict]:
    """Return { runner_label: {nodepool, nodepool_fraction, ...} }."""
    table: dict[str, dict] = {}
    for r in _load_runner_defs():
        name = r["name"]
        instance = r["instance_type"]
        fleet = derive_fleet_name(instance, r.get("node_fleet"))
        v = float(r["vcpu"])
        m = _parse_mem_gib(str(r["memory"]))
        node_v, node_m = _nodepool_capacity(instance)
        frac = _pick_fraction(v, m, node_v, node_m)
        table[name] = {
            "nodepool": fleet,
            "instance_type": instance,
            "nodepool_fraction": frac,
            "vcpu": v,
            "memory_gib": m,
            "node_vcpu": node_v,
            "node_mem_gib": node_m,
            "gpu": int(r.get("gpu", 0)),
        }
    return table


def _strip_provider_prefix(label: str) -> tuple[str, str] | None:
    """Given a HUD label ('mt-...', 'lf-...'), return (provider, osdc_runner_name).

    Handles:
      * 'mt-l-x86...'   -> ('mt', 'l-x86...')   (new naming)
      * 'lf-l-x86...'   -> ('lf', 'l-x86...')
      * 'mt-rel-l-...'  -> ('mt', 'rel-l-...')
      * 'mt-linux.2xl'  -> ('mt', mapped via OLD_TO_NEW_LABEL)
      * 'lf-linux.2xl'  -> ('lf', mapped via OLD_TO_NEW_LABEL)

    Returns None if not a mt-/lf- label or the mapping is unknown.
    """
    if label.startswith("mt-"):
        provider = "mt"
    elif label.startswith("lf-"):
        provider = "lf"
    else:
        return None
    rest = label.split("-", 1)[1]  # drop 'mt' or 'lf'
    if rest.startswith(("l-", "rel-l-")):
        return provider, rest
    # Legacy 'linux.*' names — translate via OLD_TO_NEW_LABEL.
    mapped = OLD_TO_NEW_LABEL.get(rest)
    if mapped is None:
        return None
    return provider, mapped


def _bucket_start(iso_str: str) -> str:
    """Round an ISO timestamp DOWN to the nearest 5-min bucket."""
    # Accept 'YYYY-MM-DDTHH:MM:SS' (ClickHouse DateTime cast).
    t = dt.datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.UTC)
    bucketed = t.replace(minute=(t.minute // 5) * 5, second=0, microsecond=0)
    return bucketed.strftime("%Y-%m-%dT%H:%M:%S%z")


def _to_utc_str(iso_str: str) -> str:
    t = dt.datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.UTC)
    return t.strftime("%Y-%m-%dT%H:%M:%S%z")


def _iter_chunk_rows(paths: list[Path]):
    """Yield [label, started_at, completed_at, runtime_s] rows across all chunks."""
    for p in paths:
        rows = json.loads(p.read_text())
        yield from rows


def cmd_build_table(_args) -> int:
    table = build_label_table()
    json.dump(table, sys.stdout, indent=2, sort_keys=True)
    print()
    return 0


def cmd_extract(args) -> int:
    chunk_paths = [Path(p) for p in args.chunks]
    for p in chunk_paths:
        if not p.is_file():
            print(f"ERROR: chunk not found: {p}", file=sys.stderr)
            return 2
    table = build_label_table()
    unknown: dict[str, int] = {}
    kept = 0
    skipped_runtime = 0
    skipped_unknown = 0

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["provider", "label", "nodepool", "nodepool_fraction", "start_time", "end_time"])
        for hud_label, started, completed, runtime_s in _iter_chunk_rows(chunk_paths):
            if not (MIN_RUNTIME_S <= int(runtime_s) <= MAX_RUNTIME_S):
                skipped_runtime += 1
                continue
            parsed = _strip_provider_prefix(hud_label)
            if parsed is None or parsed[1] not in table:
                unknown[hud_label] = unknown.get(hud_label, 0) + 1
                skipped_unknown += 1
                continue
            provider, osdc_label = parsed
            row = table[osdc_label]
            w.writerow(
                [
                    provider,
                    osdc_label,
                    row["nodepool"],
                    row["nodepool_fraction"],
                    _bucket_start(started),
                    _to_utc_str(completed),
                ]
            )
            kept += 1

    print(f"wrote {out}", file=sys.stderr)
    print(f"  kept:            {kept}", file=sys.stderr)
    print(f"  skipped runtime: {skipped_runtime}", file=sys.stderr)
    print(f"  skipped unknown: {skipped_unknown}", file=sys.stderr)
    if unknown:
        top = sorted(unknown.items(), key=lambda kv: -kv[1])[:20]
        print(f"  top unknown labels ({len(unknown)} distinct):", file=sys.stderr)
        for lbl, n in top:
            print(f"    {n:>6}  {lbl}", file=sys.stderr)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_table = sub.add_parser("build-table", help="Print label -> nodepool mapping as JSON")
    p_table.set_defaults(func=cmd_build_table)

    p_ext = sub.add_parser("extract", help="Filter + join HUD chunks -> CSV")
    p_ext.add_argument("--out", required=True, help="output CSV path")
    p_ext.add_argument("chunks", nargs="+", help="raw ClickHouse JSON chunk file(s)")
    p_ext.set_defaults(func=cmd_extract)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
