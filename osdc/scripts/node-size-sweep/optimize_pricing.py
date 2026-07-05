"""Dollar-cost pricing layer for the node-size optimizer.

Turns node-count bucket outputs into USD/hr using a static on-demand price
table (``optimize_pricing_data.json``) joined to the hardware specs in
``scripts.python.instance_specs``. Reserved Capacity-Block families (p5,
p6-b200) are intentionally absent from the price table and surface as
"unpriced" everywhere — they contribute to node counts but never to dollars.

Public API consumed by later optimizer phases:
    REGIONS, load_prices, hourly_price, blended_price, region_available,
    family_cost_efficiency, select_candidate_families, cost_of_node_counts
"""

from __future__ import annotations

import copy
import functools
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts" / "python"))

from instance_specs import INSTANCE_SPECS  # noqa: E402
from nodepool_defs import load_excluded_instance_types  # noqa: E402

REGIONS: tuple[str, ...] = ("us-east-1", "us-east-2", "us-west-1")

DATA_PATH = Path(__file__).resolve().parent / "optimize_pricing_data.json"
DEFS_DIR = REPO_ROOT / "modules" / "nodepools" / "defs"


@functools.cache
def _load_prices_cached(path_str: str) -> dict:
    with Path(path_str).open() as fh:
        return json.load(fh)


def load_prices(path: str | Path | None = None) -> dict:
    """Load the price-table JSON. Defaults to the sibling data file.

    Returns a fresh deep copy on every call so callers may mutate the result
    without poisoning the shared parse cache for later callers.
    """
    target = Path(path) if path is not None else DATA_PATH
    return copy.deepcopy(_load_prices_cached(str(target.resolve())))


def _prices_map(prices: dict | None = None) -> dict:
    """Return the ``instance_type -> {region: price}`` map.

    Accepts either a full loaded table (with a top-level ``prices`` key), a
    bare inner map, or None (load the default table).
    """
    if prices is None:
        prices = load_prices()
    if isinstance(prices, dict) and isinstance(prices.get("prices"), dict):
        return prices["prices"]
    return prices


def hourly_price(instance_type: str, region: str, prices: dict | None = None) -> float | None:
    """On-demand USD/hr for one instance type in one region.

    Returns None when the type is unpriced (reserved/absent) or has no price
    in that region (e.g. a family excluded from the region).
    """
    entry = _prices_map(prices).get(instance_type)
    if not entry:
        return None
    return entry.get(region)


def blended_price(instance_type: str, prices: dict | None = None) -> float | None:
    """Mean of the non-null REGIONS prices for a type; None if all null/absent."""
    entry = _prices_map(prices).get(instance_type)
    if not entry:
        return None
    vals = [entry.get(r) for r in REGIONS]
    vals = [v for v in vals if v is not None]
    if not vals:
        return None
    return sum(vals) / len(vals)


@functools.cache
def _excluded_families_for_region(region: str) -> frozenset[str]:
    types = load_excluded_instance_types(DEFS_DIR, region)
    return frozenset(t.split(".")[0] for t in types)


def region_available(instance_type: str, region: str) -> bool:
    """True if the instance's family is not region-excluded by its nodepool def.

    ``exclude_regions`` is a fleet-level (family-level) property in the def
    YAMLs, so availability is resolved per family. Parses are cached per region.
    """
    family = instance_type.split(".")[0]
    return family not in _excluded_families_for_region(region)


@functools.cache
def _families() -> dict[str, tuple[str, ...]]:
    grouped: dict[str, list[str]] = {}
    for itype in INSTANCE_SPECS:
        grouped.setdefault(itype.split(".")[0], []).append(itype)
    return {family: tuple(sorted(members)) for family, members in grouped.items()}


@functools.cache
def _family_arch(family: str) -> str:
    return INSTANCE_SPECS[_families()[family][0]]["arch"]


@functools.cache
def _family_is_cpu_only(family: str) -> bool:
    return all(INSTANCE_SPECS[i]["gpu"] == 0 for i in _families()[family])


def _family_representative(family: str, prices: dict | None = None) -> str | None:
    """Largest priceable instance in a family (max vCPU, then memory, then name).

    AWS prices ~linearly within a family, so per-vCPU/per-GiB unit cost is
    near-constant across sizes; the largest size is the unit the fleet actually
    provisions and avoids small-instance rounding, so it is the representative.
    """
    pmap = _prices_map(prices)
    candidates = [
        (INSTANCE_SPECS[i]["vcpu"], INSTANCE_SPECS[i]["memory_gib"], i)
        for i in _families()[family]
        if blended_price(i, pmap) is not None
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda c: (-c[0], -c[1], c[2]))
    return candidates[0][2]


def family_cost_efficiency(prices: dict | None = None) -> dict[str, dict[str, float]]:
    """Per-family ``{"usd_per_vcpu", "usd_per_gib"}`` from the representative instance.

    Families with no priceable instance (reserved Capacity-Block families) are
    omitted. See :func:`_family_representative` for the representative choice.
    """
    pmap = _prices_map(prices)
    out: dict[str, dict[str, float]] = {}
    for family in _families():
        rep = _family_representative(family, pmap)
        if rep is None:
            continue
        blended = blended_price(rep, pmap)
        spec = INSTANCE_SPECS[rep]
        out[family] = {
            "usd_per_vcpu": blended / spec["vcpu"],
            "usd_per_gib": blended / spec["memory_gib"],
        }
    return out


def select_candidate_families(
    arch_allowlist: list[str] | tuple[str, ...],
    require_all_regions: bool = True,
    prices: dict | None = None,
) -> list[str]:
    """CPU-only families ranked cheapest: top-3 by $/vCPU UNION top-3 by $/GiB.

    Filters to gpu==0 families whose arch is in ``arch_allowlist``. When
    ``require_all_regions`` is set, families excluded from any REGION are
    dropped (drops c7a and r7a, which exclude us-west-1). Result sorted by name.
    """
    pmap = _prices_map(prices)
    arch_set = set(arch_allowlist)
    eff = family_cost_efficiency(pmap)
    eligible: list[str] = []
    for family in eff:
        if not _family_is_cpu_only(family):
            continue
        if _family_arch(family) not in arch_set:
            continue
        if require_all_regions and not all(region_available(f"{family}.", r) for r in REGIONS):
            continue
        eligible.append(family)
    top_vcpu = sorted(eligible, key=lambda f: (eff[f]["usd_per_vcpu"], f))[:3]
    top_gib = sorted(eligible, key=lambda f: (eff[f]["usd_per_gib"], f))[:3]
    return sorted(set(top_vcpu) | set(top_gib))


def cost_of_node_counts(
    counts_by_type: dict[str, float],
    region: str,
    prices: dict | None = None,
) -> dict:
    """Cost a bucket of node counts. ``region`` may be a REGION or "blended".

    Unpriced instances (reserved families, or a type absent in ``region``)
    contribute to node counts but never to dollars; their type names are
    surfaced in ``unpriced_types``.
    """
    pmap = _prices_map(prices)
    priced_usd = 0.0
    priced_nodes = 0.0
    unpriced_nodes = 0.0
    unpriced_types: set[str] = set()
    for itype, count in counts_by_type.items():
        price = blended_price(itype, pmap) if region == "blended" else hourly_price(itype, region, pmap)
        if price is None:
            unpriced_nodes += count
            unpriced_types.add(itype)
        else:
            priced_nodes += count
            priced_usd += count * price
    return {
        "priced_usd_per_hr": priced_usd,
        "priced_node_count": priced_nodes,
        "unpriced_node_count": unpriced_nodes,
        "unpriced_types": sorted(unpriced_types),
    }
