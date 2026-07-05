"""Node-hours and dollar-cost post-processing for the node-size optimizer.

Pure post-processing of the simulator's per-bucket node-count outputs: no sim,
no network, no I/O. Relative figures (percentage/ratio deltas) are trustworthy;
absolute dollars are on-demand list price times sim node-hours, and sim
node-hours are themselves a lower bound, so absolute $ is only approximate.
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
if str(REPO_ROOT / "scripts" / "python") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts" / "python"))

from optimize_pricing import REGIONS, _prices_map, cost_of_node_counts, load_prices  # noqa: E402
from simulate import BUCKET_SEC  # noqa: E402

BUCKET_HOURS = BUCKET_SEC / 3600.0

_AS_OF = load_prices().get("_as_of")

NODE_HOURS_CAVEAT = "lower bound (sim empties linger 10min vs Karpenter 3h)"
ABS_USD_CAVEAT = "approximate (on-demand list price; sim node-hours are a lower bound)" + (
    f"; prices as of {_AS_OF}" if _AS_OF else ""
)
DENOM_NOTE = "of priced spend, excludes reserved GPU"


def node_hours_and_cost(sim_out: dict, region: str = "blended", prices: dict | None = None) -> dict:
    """Aggregate a sim run's per-bucket node counts into node-hours and USD.

    Each bucket contributes ``count * BUCKET_HOURS`` node-hours per instance
    type. Priced and unpriced (reserved/absent) node-hours are tracked
    separately; only priced node-hours drive the USD total.
    """
    if region not in (*REGIONS, "blended"):
        raise ValueError(f"unknown region: {region}")
    pmap = _prices_map(prices)
    priced_nh = 0.0
    unpriced_nh = 0.0
    usd = 0.0
    unpriced_types: set[str] = set()
    per_pool: dict[str, dict] = {}

    for _t, pools in sim_out.get("per_bucket", []):
        for pool_name, sums in pools.items():
            counts = sums.get("node_counts_by_type") or {}
            clean_counts: dict[str, float] = {}
            none_count = 0.0
            for itype, cnt in counts.items():
                # None instance_type would crash cost_of_node_counts' sorted(unpriced_types);
                # count it as unpriced node-hours without naming it a type.
                if itype is None:
                    none_count += cnt
                else:
                    clean_counts[itype] = clean_counts.get(itype, 0.0) + cnt

            cost = cost_of_node_counts(clean_counts, region, pmap)
            pool_priced_nh = cost["priced_node_count"] * BUCKET_HOURS
            pool_unpriced_nh = (cost["unpriced_node_count"] + none_count) * BUCKET_HOURS
            pool_usd = cost["priced_usd_per_hr"] * BUCKET_HOURS

            priced_nh += pool_priced_nh
            unpriced_nh += pool_unpriced_nh
            usd += pool_usd
            unpriced_types.update(cost["unpriced_types"])

            acc = per_pool.setdefault(
                pool_name,
                {"node_hours": 0.0, "usd": 0.0, "unpriced_node_hours": 0.0, "_unpriced_types": set()},
            )
            acc["node_hours"] += pool_priced_nh + pool_unpriced_nh
            acc["usd"] += pool_usd
            acc["unpriced_node_hours"] += pool_unpriced_nh
            acc["_unpriced_types"].update(cost["unpriced_types"])

    per_pool_out = {
        name: {
            "node_hours": acc["node_hours"],
            "usd": acc["usd"],
            "unpriced_node_hours": acc["unpriced_node_hours"],
            "unpriced_types": sorted(acc["_unpriced_types"]),
        }
        for name, acc in per_pool.items()
    }

    return {
        "region": region,
        "node_hours": priced_nh + unpriced_nh,
        "priced_node_hours": priced_nh,
        "unpriced_node_hours": unpriced_nh,
        "usd": usd,
        "unpriced_types": sorted(unpriced_types),
        "per_pool": per_pool_out,
    }


def savings(baseline_cost: dict, rec_cost: dict) -> dict:
    """Fractional savings of recommendation vs baseline (positive == saved).

    A zero/falsy baseline yields None for that fraction rather than raising.
    """

    def fraction(key: str) -> float | None:
        base = baseline_cost.get(key, 0.0)
        if not base:
            return None
        return (base - rec_cost.get(key, 0.0)) / base

    return {
        "pct_node_hours": fraction("node_hours"),
        "pct_usd_priced": fraction("usd"),
        "denominator_note": DENOM_NOTE,
    }


def _fmt_pct(pct: float | None) -> str:
    return "n/a" if pct is None else f"{pct:+.1f}%"


def _family_reserved_line(cost: dict, indent: str) -> str:
    return (
        f"{indent}reserved/unpriced node_hours ~ {cost['unpriced_node_hours']:,.0f} "
        f"(types: {', '.join(cost['unpriced_types'])}) — excluded from $"
    )


def family_baseline_cost_lines(cost: dict | None, indent: str = "  ") -> list[str]:
    """Render the per-family baseline cost block. Empty list when cost is None."""
    if cost is None:
        return []
    lines = [
        f"{indent}node_hours ~ {cost['node_hours']:,.0f} ({NODE_HOURS_CAVEAT})",
        f"{indent}approx cost ~ ${cost['usd']:,.0f}/window — {ABS_USD_CAVEAT}",
    ]
    if cost["unpriced_node_hours"] > 0:
        lines.append(_family_reserved_line(cost, indent))
    return lines


def family_rec_cost_lines(baseline_cost: dict | None, rec_cost: dict | None, indent: str = "  ") -> list[str]:
    """Render the per-family recommendation cost block. Empty list when rec_cost is None.

    Displayed percentages are the CHANGE (rec-baseline)/baseline so negative
    means a reduction, matching the existing vcpu_hours row style.
    """
    if rec_cost is None:
        return []
    lines: list[str] = []
    if baseline_cost is not None:
        s = savings(baseline_cost, rec_cost)
        nh_delta = rec_cost["node_hours"] - baseline_cost["node_hours"]
        pct_nh = s["pct_node_hours"]
        nh_pct = -pct_nh * 100 if pct_nh is not None else None
        base_usd = baseline_cost["usd"]
        usd_pct = 100.0 * (rec_cost["usd"] - base_usd) / base_usd if base_usd else None
        lines.append(
            f"{indent}node_hours ~ {rec_cost['node_hours']:,.0f} "
            f"[{nh_delta:+,.0f} vs baseline, {_fmt_pct(nh_pct)}] ({NODE_HOURS_CAVEAT})"
        )
        lines.append(f"{indent}priced-$ change: {_fmt_pct(usd_pct)} — {ABS_USD_CAVEAT}")
        lines.append(f"{indent}approx cost ~ ${rec_cost['usd']:,.0f}/window — {ABS_USD_CAVEAT}")
        lines.append(f"{indent}(node-hours x price = authoritative cost; vcpu_hours is a compute proxy)")
    else:
        lines.append(f"{indent}node_hours ~ {rec_cost['node_hours']:,.0f} ({NODE_HOURS_CAVEAT})")
        lines.append(f"{indent}approx cost ~ ${rec_cost['usd']:,.0f}/window — {ABS_USD_CAVEAT}")
    if rec_cost["unpriced_node_hours"] > 0:
        lines.append(_family_reserved_line(rec_cost, indent))
    return lines


def cluster_cost_lines(baseline_cost: dict | None, rec_cost: dict | None) -> list[str]:
    """Render the cluster-wide cost bullets. Empty list when either cost is None.

    "saved" is positive when the recommendation reduces spend, so the savings()
    fractions are used directly.
    """
    if baseline_cost is None or rec_cost is None:
        return []
    s = savings(baseline_cost, rec_cost)
    nh_pct = s["pct_node_hours"]
    usd_pct = s["pct_usd_priced"]
    nh_saved = _fmt_pct(nh_pct * 100 if nh_pct is not None else None)
    usd_saved = _fmt_pct(usd_pct * 100 if usd_pct is not None else None)

    reserved = (
        f"- reserved/unpriced node-hours (NOT in priced total): "
        f"baseline {baseline_cost['unpriced_node_hours']:,.0f}, rec {rec_cost['unpriced_node_hours']:,.0f}"
    )
    all_types = sorted(set(baseline_cost["unpriced_types"]) | set(rec_cost["unpriced_types"]))
    if all_types:
        reserved += f" (types: {', '.join(all_types)})"

    return [
        "",
        "### Cost (relative — absolute $ is approximate on-demand list price)",
        f"- % node-hours saved: {nh_saved} "
        f"(baseline {baseline_cost['node_hours']:,.0f} -> rec {rec_cost['node_hours']:,.0f} node-hours; "
        f"{NODE_HOURS_CAVEAT})",
        f"- % priced-$ saved: {usd_saved} ({DENOM_NOTE})",
        reserved,
        f"- absolute $ ({ABS_USD_CAVEAT}): baseline ~${baseline_cost['usd']:,.0f}, rec ~${rec_cost['usd']:,.0f}",
    ]
