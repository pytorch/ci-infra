"""Per-family and global report writers + git-apply-able patch generation."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from optimize_catalog import EligibleEntry
    from optimize_engine import Config, FamilyResult
    from optimize_storage import ClusterValidationResult, SimMetrics


def _config_pods_by_sub(config: "Config") -> list[tuple[str, str, list[str]]]:
    """Return sorted [(sub_id, instance, pods)] rows for stable output."""
    rows: list[tuple[str, str, list[str]]] = []
    for sub_id in sorted(config):
        spec = config[sub_id]
        rows.append((sub_id, spec["instance"], sorted(spec["pods"])))
    return rows


def _def_shape_row(
    def_label: str,
    orig_cpu_m: int,
    orig_mem_mi: int,
    slot_cpu_m: int,
    slot_mem_mi: int,
    n: int,
) -> str:
    cpu_pct = 100.0 * (slot_cpu_m - orig_cpu_m) / orig_cpu_m if orig_cpu_m else 0.0
    mem_pct = 100.0 * (slot_mem_mi - orig_mem_mi) / orig_mem_mi if orig_mem_mi else 0.0
    return (
        f"    {def_label} "
        f"(adj: {orig_cpu_m / 1000:.1f}c -> {slot_cpu_m / 1000:.1f}c {cpu_pct:+.1f}%, "
        f"{orig_mem_mi / 1024:.1f}Gi -> {slot_mem_mi / 1024:.1f}Gi {mem_pct:+.1f}%) N/node={n}"
    )


def _append_cluster_contribution_section(lines: list[str], r: "FamilyResult") -> None:
    """Emit the family's SHARE of the full-cluster before/after sim.

    Extracted from the same two cluster sims that produce the global
    Cluster-wide validation table — pool_filter is
    baseline: the family's original nodepool names; rec: sub_nodepool_ids
    from r.best_config for improved families, else the baseline names.
    """
    bf = r.cluster_baseline_metrics
    ef = r.cluster_rec_metrics
    if bf is None and ef is None:
        return
    lines.append("## Cluster contribution (this family's share of the full-cluster sim)")
    if bf is not None:
        lines.append("  Baseline (full cluster):")
        lines.append(
            f"    opt_max = {bf.opt_max * 100:.1f}% "
            f"(cpu {bf.opt_cpu * 100:.1f}%, mem {bf.opt_mem * 100:.1f}%)"
        )
        lines.append(f"    cal_cpu = {bf.cal_cpu * 100:.1f}%, cal_mem = {bf.cal_mem * 100:.1f}%")
        lines.append(f"    vcpu_hours ~ {bf.vcpu_hours:,.0f}")
    if ef is not None:
        lines.append("  Recommendation (full cluster):")
        if bf is not None:
            delta_pp = (ef.opt_max - bf.opt_max) * 100.0
            lines.append(
                f"    opt_max = {ef.opt_max * 100:.1f}% "
                f"(cpu {ef.opt_cpu * 100:.1f}%, mem {ef.opt_mem * 100:.1f}%) "
                f"[{delta_pp:+.2f}pp vs baseline]"
            )
        else:
            lines.append(
                f"    opt_max = {ef.opt_max * 100:.1f}% "
                f"(cpu {ef.opt_cpu * 100:.1f}%, mem {ef.opt_mem * 100:.1f}%)"
            )
        lines.append(f"    cal_cpu = {ef.cal_cpu * 100:.1f}%, cal_mem = {ef.cal_mem * 100:.1f}%")
        if bf is not None and bf.vcpu_hours > 0:
            vh_delta = ef.vcpu_hours - bf.vcpu_hours
            pct = 100.0 * vh_delta / bf.vcpu_hours
            lines.append(
                f"    vcpu_hours ~ {ef.vcpu_hours:,.0f} [{vh_delta:+,.0f} vs baseline, {pct:+.1f}%]"
            )
        else:
            lines.append(f"    vcpu_hours ~ {ef.vcpu_hours:,.0f}")
    lines.append("")


def write_family_report(
    reports_dir: Path,
    r: "FamilyResult",
    defs: list[dict],
    catalog_entries: list["EligibleEntry"],
) -> None:
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / f"{r.family}.md"

    if r.skipped_reason:
        path.write_text(
            f"# Fleet: {r.family}\n\n"
            f"## Skipped\n\nreason: {r.skipped_reason}\n\n"
            "no data available for this family in the current window.\n"
        )
        return

    catalog_by_pair = {(e.def_label, e.instance): e for e in catalog_entries}
    orig_by_def = {(e.def_label): (e.orig_cpu_m, e.orig_mem_mi) for e in catalog_entries}

    lines: list[str] = [f"# Fleet: {r.family}", ""]

    lines.append("## Baseline (current config, single sub-nodepool)")
    for sub_id, inst, pods in _config_pods_by_sub(r.baseline_config):
        lines.append(f"  sub_nodepool {sub_id} (instance {inst}):")
        lines.append(f"    pods: {', '.join(pods)}")
    if r.baseline_metrics is not None:
        lines.append(
            f"  opt_max = {r.baseline_metrics.opt_max * 100:.1f}% "
            f"(cpu {r.baseline_metrics.opt_cpu * 100:.1f}%, mem {r.baseline_metrics.opt_mem * 100:.1f}%)"
        )
        lines.append(
            f"  cal_cpu = {r.baseline_metrics.cal_cpu * 100:.1f}%, cal_mem = {r.baseline_metrics.cal_mem * 100:.1f}%"
        )
        lines.append(f"  vcpu_hours ~ {r.baseline_metrics.vcpu_hours:,.0f}")
    lines.append("")

    if r.best_config is None or r.best_metrics is None:
        lines.append("## Recommendation")
        lines.append(f"  (none — verdict: {r.verdict})")
        path.write_text("\n".join(lines) + "\n")
        return

    lines.append(f"## Recommendation ({len(r.best_config)} sub-nodepool(s))")
    for sub_id, inst, pods in _config_pods_by_sub(r.best_config):
        lines.append(f"  sub_nodepool {sub_id} (instance {inst}):")
        for pod in pods:
            orig = orig_by_def.get(pod)
            entry = catalog_by_pair.get((pod, inst))
            if orig is None or entry is None:
                lines.append(f"    {pod} (no catalog entry — check eligibility)")
                continue
            lines.append(_def_shape_row(pod, orig[0], orig[1], entry.slot_cpu_m, entry.slot_mem_mi, entry.n))
    if r.best_metrics is not None:
        delta_pp = (
            (r.best_metrics.opt_max - r.baseline_metrics.opt_max) * 100.0 if r.baseline_metrics is not None else 0.0
        )
        lines.append(
            f"  opt_max = {r.best_metrics.opt_max * 100:.1f}% "
            f"(cpu {r.best_metrics.opt_cpu * 100:.1f}%, mem {r.best_metrics.opt_mem * 100:.1f}%) "
            f"[{delta_pp:+.2f}pp vs baseline]"
        )
        lines.append(f"  cal_cpu = {r.best_metrics.cal_cpu * 100:.1f}%, cal_mem = {r.best_metrics.cal_mem * 100:.1f}%")
        base_vcpu_h = r.baseline_metrics.vcpu_hours if r.baseline_metrics is not None else 0.0
        vcpu_hours_delta = r.best_metrics.vcpu_hours - base_vcpu_h
        pct = (100.0 * vcpu_hours_delta / base_vcpu_h) if base_vcpu_h > 0 else 0.0
        lines.append(
            f"  vcpu_hours ~ {r.best_metrics.vcpu_hours:,.0f} "
            f"[{vcpu_hours_delta:+,.0f} vs baseline, {pct:+.1f}%]"
        )
    lines.append("")

    _append_cluster_contribution_section(lines, r)

    lines.append("## Convergence")
    lines.append(
        f"  mode = {r.mode}, restarts_run = {r.restarts_run}, "
        f"configs_evaluated = {r.configs_evaluated}, "
        f"cache_hit_rate = {r.cache_hit_rate:.1%}, elapsed = {r.elapsed_sec:.1f}s"
    )
    lines.append("")
    lines.append(f"## Verdict: {r.verdict}")

    path.write_text("\n".join(lines) + "\n")


# ---------- patch generation ----------

RENAME_THRESHOLD_PCT_DEFAULT = 10.0


def _classify_rename(
    orig_cpu_m: int, orig_mem_mi: int, slot_cpu_m: int, slot_mem_mi: int, threshold_pct: float
) -> bool:
    cpu_pct = abs(100.0 * (slot_cpu_m - orig_cpu_m) / orig_cpu_m) if orig_cpu_m else 0.0
    mem_pct = abs(100.0 * (slot_mem_mi - orig_mem_mi) / orig_mem_mi) if orig_mem_mi else 0.0
    return max(cpu_pct, mem_pct) > threshold_pct


def write_family_patch(
    reports_dir: Path,
    r: "FamilyResult",
    defs: list[dict],
    catalog_entries: list["EligibleEntry"],
    rename_threshold_pct: float = RENAME_THRESHOLD_PCT_DEFAULT,
) -> None:
    """Emit a stub patch describing the recommended changes.

    Full unified-diff generation against real def YAMLs is Phase 4 territory
    (D6). For now, emit an annotated summary listing:
    - New sub-nodepool YAML(s) that would need creating.
    - Per-def field changes (node_fleet, vcpu, memory).
    - Rename-required flags per def where cpu/mem adjustment > threshold.
    """
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / f"{r.family}.patch"

    if r.skipped_reason:
        path.write_text(f"# SKIPPED family {r.family}: {r.skipped_reason}\n")
        return

    if r.best_config is None or r.best_metrics is None:
        path.write_text(f"# family {r.family}: no recommendation (verdict: {r.verdict})\n")
        return

    catalog_by_pair = {(e.def_label, e.instance): e for e in catalog_entries}

    lines: list[str] = [
        f"# Family: {r.family}",
        f"# Verdict: {r.verdict}",
        "",
        "## New sub-nodepool YAMLs (create):",
    ]
    for sub_id, inst, pods in _config_pods_by_sub(r.best_config):
        yaml_name = sub_id.replace("__", "-")
        lines.append(
            f"#   modules/nodepools/defs/{yaml_name}.yaml: "
            f"fleet.name={sub_id}, instances=[{inst}], "
            f"pods=[{', '.join(pods)}]"
        )
    lines.append("")
    lines.append("## Per-def changes:")

    baseline_assignment = {}
    for sub_id, spec in r.baseline_config.items():
        for pod in spec["pods"]:
            baseline_assignment[pod] = (sub_id, spec["instance"])

    rename_flags: list[tuple[str, float, float]] = []

    for sub_id, inst, pods in _config_pods_by_sub(r.best_config):
        for pod in pods:
            entry = catalog_by_pair.get((pod, inst))
            if entry is None:
                continue
            base_sub, base_inst = baseline_assignment.get(pod, ("?", "?"))
            fields = [
                f"node_fleet: {base_sub} -> {sub_id}",
                f"vcpu: {entry.orig_cpu_m / 1000:.1f} -> {entry.slot_cpu_m / 1000:.1f}",
                f"memory_gib: {entry.orig_mem_mi / 1024:.1f} -> {entry.slot_mem_mi / 1024:.1f}",
                f"instance_type: {base_inst} -> {inst}",
            ]
            lines.append(f"#   modules/arc-runners/defs/{pod}.yaml: " + "; ".join(fields))
            if _classify_rename(
                entry.orig_cpu_m,
                entry.orig_mem_mi,
                entry.slot_cpu_m,
                entry.slot_mem_mi,
                rename_threshold_pct,
            ):
                rename_flags.append((pod, entry.adj_cpu_pct, entry.adj_mem_pct))

    if rename_flags:
        lines.append("")
        lines.append(f"## Rename required (adjustment > {rename_threshold_pct:.1f}%):")
        for pod, cpu_pct, mem_pct in rename_flags:
            lines.append(f"#   {pod}: cpu {cpu_pct:+.1f}%, mem {mem_pct:+.1f}% — label lies about shape")

    path.write_text("\n".join(lines) + "\n")


def _fmt_pp_row(label: str, base: float, rec: float) -> str:
    """Render a percentage-point row: base%, rec%, delta in pp."""
    delta_pp = (rec - base) * 100.0
    return f"| {label} | {base * 100:.1f}% | {rec * 100:.1f}% | {delta_pp:+.1f}pp |"


def _fmt_vcpu_row(label: str, base: float, rec: float) -> str:
    """Render the vcpu_hours row: absolute totals, percent delta."""
    if base > 0:
        pct = 100.0 * (rec - base) / base
        delta = f"{pct:+.1f}%"
    else:
        delta = "n/a"
    return f"| {label} | {base:,.0f} | {rec:,.0f} | {delta} |"


def _append_cluster_validation_table(
    lines: list[str],
    cluster_validation: "ClusterValidationResult",
) -> None:
    days_label = f"{cluster_validation.days}d" if cluster_validation.days else "full dataset"
    b = cluster_validation.baseline_metrics
    r = cluster_validation.recommendation_metrics
    lines.append(f"## Cluster-wide validation ({days_label} full dataset, all families)")
    lines.append("")
    lines.append("|                       | Baseline   | Recommendation | Delta       |")
    lines.append("|:----------------------|:-----------|:---------------|:------------|")
    lines.append(_fmt_pp_row("opt_max               ", b.opt_max, r.opt_max))
    lines.append(_fmt_pp_row("cpu util              ", b.opt_cpu, r.opt_cpu))
    lines.append(_fmt_pp_row("mem util              ", b.opt_mem, r.opt_mem))
    lines.append(_fmt_pp_row("cal_cpu (prod PromQL) ", b.cal_cpu, r.cal_cpu))
    lines.append(_fmt_pp_row("cal_mem (prod PromQL) ", b.cal_mem, r.cal_mem))
    lines.append(_fmt_vcpu_row("vcpu_hours            ", b.vcpu_hours, r.vcpu_hours))
    lines.append("")


def write_global_report(
    reports_dir: Path,
    results: list["FamilyResult"],
    cluster_validation: "ClusterValidationResult | None" = None,
) -> None:
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / "global.md"

    lines = [
        "# Global search summary",
        "",
        "## Search window",
        "",
        "| family | baseline opt_max | rec opt_max | delta (pp) | verdict | configs | wall (s) |",
        "|--------|-----------------:|------------:|-----------:|:--------|--------:|---------:|",
    ]
    for r in sorted(results, key=lambda x: x.family):
        if r.skipped_reason:
            lines.append(f"| {r.family} | n/a | n/a | n/a | skipped ({r.skipped_reason}) | 0 | {r.elapsed_sec:.0f} |")
            continue
        if r.best_metrics is None or r.baseline_metrics is None:
            lines.append(
                f"| {r.family} | n/a | n/a | n/a | {r.verdict} | {r.configs_evaluated} | {r.elapsed_sec:.0f} |"
            )
            continue
        delta_pp = (r.best_metrics.opt_max - r.baseline_metrics.opt_max) * 100.0
        lines.append(
            f"| {r.family} | {r.baseline_metrics.opt_max * 100:.1f}% | "
            f"{r.best_metrics.opt_max * 100:.1f}% | {delta_pp:+.2f} | "
            f"{r.verdict} | {r.configs_evaluated} | {r.elapsed_sec:.0f} |"
        )
    lines.append("")

    if cluster_validation is not None:
        _append_cluster_validation_table(lines, cluster_validation)

    path.write_text("\n".join(lines) + "\n")
