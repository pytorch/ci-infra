"""Runner-fleet host-optimization report writer (Phase 2.5)."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from optimize_runner_fleet import CandidateResult, RunnerFleetResult


ARM_GATED_MARKER = "GATED — not yet deployable, see arm64 note"


def _fmt_price(value: float | None) -> str:
    return f"${value:.3f}" if value is not None else "n/a"


def _fmt_ratio_pct(ratio: float | None) -> str:
    return f"{(ratio - 1.0) * 100:+.1f}%" if ratio is not None else "n/a"


def _runner_candidate_row(c: CandidateResult, regions: tuple[str, ...]) -> str:
    region_cells = " | ".join(_fmt_price(c.region_prices.get(r)) for r in regions)
    return (
        f"| {c.instance_type} | {c.arch} | {c.slots} | {c.node_hours:,.1f} | "
        f"{region_cells} | {_fmt_price(c.blended_price)} | {_fmt_ratio_pct(c.cost_ratio)} |"
    )


def _append_runner_winner(lines: list[str], label: str, c: CandidateResult | None, caveat: str | None) -> None:
    if c is None:
        lines.append(f"- {label}: none")
        return
    tag = f" ({caveat})" if caveat else ""
    lines.append(
        f"- {label}: **{c.instance_type}** ({c.arch}) — {_fmt_ratio_pct(c.cost_ratio)} vs baseline, "
        f"{c.slots} slots/node, ~{c.node_hours:,.0f} node-hours{tag}"
    )


def _arm_caveat_suffix(arch: str | None, deployable_arch: str) -> str:
    return f" ({ARM_GATED_MARKER})" if arch is not None and arch != deployable_arch else ""


def write_runner_fleet_report(reports_dir: Path, result: RunnerFleetResult) -> None:
    """Write the closed-form runner-pod host ranking + dual winner section."""
    from optimize_pricing import REGIONS, load_prices
    from optimize_runner_fleet import ARM_CAVEAT, DEPLOYABLE_ARCH

    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / "runner_fleet.md"
    regions = tuple(REGIONS)
    as_of = load_prices().get("_as_of")
    as_of_note = f" Prices as of {as_of}." if as_of else ""

    lines: list[str] = [
        "# Runner-fleet host optimization (Phase 2.5)",
        "",
        "Cheapest instance to host the fixed 750m/1Gi ARC runner pods on the "
        f"`{result.baseline_instance.split('.')[0]}-runner` fleet. Closed-form: "
        "`slots = min(allocatable_cpu_m // runner_cpu_m, allocatable_mem_mi // runner_mem_mi)` "
        "(CPU is the binding constraint for the current 750m/1Gi shape). Node-hours modeled "
        f"with a {result.window_buckets}-bucket (3h) Karpenter consolidation lag.",
        "",
        f"- runner pod: {result.runner_cpu_m}m CPU / {result.runner_mem_mi}Mi mem",
        f"- peak concurrency: {result.peak_concurrency:,} runner pods across {result.total_buckets:,} buckets",
        f"- baseline: {result.baseline_instance} ({result.baseline_slots} slots/node, "
        f"~{result.baseline_node_hours:,.0f} node-hours)",
        "",
        "Reserved GPU families are irrelevant here — all candidates are CPU-only.",
        "",
    ]

    lines.append("## Global winner")
    lines.append("")
    if result.global_winner is not None:
        gw = result.global_winner
        gw_caveat = _arm_caveat_suffix(gw.arch, DEPLOYABLE_ARCH)
        lines.append(f"- **{gw.instance_type}** ({gw.arch}) — {_fmt_ratio_pct(gw.cost_ratio)} vs baseline{gw_caveat}")
    else:
        lines.append("- none")
    lines.append("")

    lines.append("## Dual winner")
    lines.append("")
    _append_runner_winner(lines, "best amd64 (ACTIONABLE today)", result.best_amd64, None)
    _append_runner_winner(lines, "best arm64 (GATED)", result.best_arm64, ARM_CAVEAT)
    lines.append("")

    lines.append("## Global vs per-region")
    lines.append("")
    arch_by_instance = {c.instance_type: c.arch for c in result.ranked}
    if result.region_winners:
        for w in result.region_winners:
            rw_caveat = _arm_caveat_suffix(arch_by_instance.get(w.instance_type), DEPLOYABLE_ARCH)
            lines.append(f"- {w.region}: {w.instance_type} (${w.cost:,.2f} approx){rw_caveat}")
    if result.global_winner is not None and result.global_penalty_ratio is not None:
        gp_caveat = _arm_caveat_suffix(result.global_winner.arch, DEPLOYABLE_ARCH)
        lines.append(
            f"- forcing one global instance ({result.global_winner.instance_type}) costs "
            f"{_fmt_ratio_pct(result.global_penalty_ratio)} vs per-region-optimal "
            f"(~${result.global_penalty_abs:,.2f} approx){gp_caveat}"
        )
    lines.append("")

    lines.append("## Ranked candidates")
    lines.append("")
    lines.append(
        "Relative to baseline; absolute $ are approximate (on-demand list; "
        "node-hours modeled with 3h consolidation lag)." + as_of_note
    )
    lines.append("")
    header = "| instance | arch | slots | node-hours | " + " | ".join(regions) + " | blended | vs baseline |"
    sep = "|:---------|:-----|------:|-----------:|" + "".join("------:|" for _ in regions) + "--------:|------------:|"
    lines.append(header)
    lines.append(sep)
    for c in result.ranked:
        lines.append(_runner_candidate_row(c, regions))
    lines.append("")

    path.write_text("\n".join(lines) + "\n")
