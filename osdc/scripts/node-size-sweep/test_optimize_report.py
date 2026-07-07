"""Unit tests for optimize_report (family/global/runner-fleet report + patch writers)."""

from __future__ import annotations

import optimize_report as rep
import optimize_report_runner as runner_rep
from optimize_catalog import EligibleEntry
from optimize_engine import FamilyResult
from optimize_pricing import REGIONS, load_prices
from optimize_runner_fleet import ARM_CAVEAT, CandidateResult, RegionWinner, RunnerFleetResult
from optimize_storage import ClusterValidationResult, SimMetrics


def _sm(
    opt_max: float = 0.80,
    opt_cpu: float = 0.78,
    opt_mem: float = 0.80,
    cal_cpu: float = 0.70,
    cal_mem: float = 0.72,
    vcpu_hours: float = 800.0,
) -> SimMetrics:
    return SimMetrics(
        opt_max=opt_max, opt_cpu=opt_cpu, opt_mem=opt_mem, cal_cpu=cal_cpu, cal_mem=cal_mem, vcpu_hours=vcpu_hours
    )


def _cost(
    node_hours: float = 100.0,
    usd: float = 1000.0,
    unpriced_node_hours: float = 0.0,
    unpriced_types: list[str] | None = None,
) -> dict:
    return {
        "node_hours": node_hours,
        "usd": usd,
        "unpriced_node_hours": unpriced_node_hours,
        "unpriced_types": unpriced_types if unpriced_types is not None else [],
    }


def _entry(
    def_label: str,
    instance: str = "g5.4xlarge",
    *,
    orig_main_vcpu: int = 16,
    new_main_vcpu: int = 20,
    orig_main_memory_gib: int = 64,
    new_main_memory_gib: int = 64,
    adj_cpu_pct: float = 25.0,
    adj_mem_pct: float = 0.0,
    n: int = 1,
) -> EligibleEntry:
    return EligibleEntry(
        def_label=def_label,
        instance=instance,
        n=n,
        slot_cpu_m=20320,
        slot_mem_mi=65856,
        slot_gpu=1,
        orig_cpu_m=16320,
        orig_mem_mi=65536,
        orig_gpu=1,
        new_main_vcpu=new_main_vcpu,
        orig_main_vcpu=orig_main_vcpu,
        new_main_memory_gib=new_main_memory_gib,
        orig_main_memory_gib=orig_main_memory_gib,
        adj_cpu_pct=adj_cpu_pct,
        adj_mem_pct=adj_mem_pct,
    )


def _family(**over) -> FamilyResult:
    base = {
        "family": "g5",
        "baseline_config": {"g5": {"instance": "g5.4xlarge", "pods": ["def-a"]}},
        "baseline_metrics": _sm(),
        "best_config": None,
        "best_metrics": None,
        "verdict": "unchanged",
    }
    base.update(over)
    return FamilyResult(**base)


def _cand(
    instance_type: str,
    arch: str,
    *,
    slots: int = 254,
    node_hours: float = 100.0,
    blended_price: float | None = 8.5,
    blended_cost: float | None = 850.0,
    cost_ratio: float | None = 0.90,
    region_prices: dict | None = None,
    region_costs: dict | None = None,
) -> CandidateResult:
    return CandidateResult(
        instance_type=instance_type,
        family=instance_type.split(".")[0],
        arch=arch,
        slots=slots,
        node_hours=node_hours,
        blended_price=blended_price,
        blended_cost=blended_cost,
        region_prices=region_prices if region_prices is not None else dict.fromkeys(REGIONS, 8.0),
        region_costs=region_costs if region_costs is not None else dict.fromkeys(REGIONS, 800.0),
        cost_ratio=cost_ratio,
    )


def _fleet(**over) -> RunnerFleetResult:
    base = {
        "arch_allowlist": ("amd64", "arm64"),
        "peak_concurrency": 100,
        "total_buckets": 10,
        "window_buckets": 36,
        "runner_cpu_m": 750,
        "runner_mem_mi": 1024,
        "baseline_instance": "c7i.48xlarge",
        "baseline_slots": 254,
        "baseline_node_hours": 1000.0,
        "baseline_blended_cost": 8500.0,
    }
    base.update(over)
    return RunnerFleetResult(**base)


def _assert_as_of_note(text: str) -> None:
    as_of = load_prices().get("_as_of")
    if as_of:
        assert f"Prices as of {as_of}." in text
    else:
        assert "Prices as of" not in text


# ---------- _def_shape_row ----------


def test_def_shape_row_main_knob_path():
    row = rep._def_shape_row(
        "def-a",
        16320,
        65536,
        20320,
        65856,
        1,
        orig_main_vcpu=16,
        new_main_vcpu=20,
        orig_main_memory_gib=64,
        new_main_memory_gib=64,
    )
    assert "main vcpu: 16 -> 20 (+25.0%)" in row
    assert "main mem: 64 -> 64 Gi (+0.0%)" in row
    assert "N/node=1" in row


def test_def_shape_row_total_fallback_when_main_none():
    row = rep._def_shape_row("def-x", 16000, 65536, 20000, 65536, 2)
    assert "total cpu:" in row
    assert "total mem:" in row
    assert "N/node=2" in row


def test_def_shape_row_zero_orig_totals_guard():
    row = rep._def_shape_row("def-z", 0, 0, 1000, 1024, 1)
    assert "total cpu: 0.0c -> 1.0c (+0.0%)" in row
    assert "total mem: 0.0Gi -> 1.0Gi (+0.0%)" in row


def test_def_shape_row_zero_main_knob_guard():
    row = rep._def_shape_row(
        "d",
        16000,
        65536,
        20000,
        65536,
        1,
        orig_main_vcpu=0,
        new_main_vcpu=4,
        orig_main_memory_gib=0,
        new_main_memory_gib=8,
    )
    assert "main vcpu: 0 -> 4 (+0.0%)" in row
    assert "main mem: 0 -> 8 Gi (+0.0%)" in row


# ---------- _append_cluster_contribution_section ----------


def test_cluster_contribution_both_none_is_noop():
    lines: list[str] = []
    r = _family(cluster_baseline_metrics=None, cluster_rec_metrics=None)
    rep._append_cluster_contribution_section(lines, r)
    assert lines == []


def test_cluster_contribution_baseline_and_rec():
    lines: list[str] = []
    r = _family(
        cluster_baseline_metrics=_sm(opt_max=0.60, vcpu_hours=1000.0),
        cluster_rec_metrics=_sm(opt_max=0.75, vcpu_hours=900.0),
    )
    rep._append_cluster_contribution_section(lines, r)
    text = "\n".join(lines)
    assert "## Cluster contribution" in text
    assert "Baseline (full cluster):" in text
    assert "Recommendation (full cluster):" in text
    assert "vs baseline" in text
    assert "-10.0%" in text


def test_cluster_contribution_rec_only_no_baseline():
    lines: list[str] = []
    r = _family(cluster_baseline_metrics=None, cluster_rec_metrics=_sm(opt_max=0.75, vcpu_hours=900.0))
    rep._append_cluster_contribution_section(lines, r)
    text = "\n".join(lines)
    assert "Recommendation (full cluster):" in text
    assert "Baseline (full cluster):" not in text
    assert "vs baseline" not in text


def test_cluster_contribution_baseline_zero_vcpu_hours():
    lines: list[str] = []
    r = _family(
        cluster_baseline_metrics=_sm(vcpu_hours=0.0),
        cluster_rec_metrics=_sm(vcpu_hours=900.0),
    )
    rep._append_cluster_contribution_section(lines, r)
    text = "\n".join(lines)
    # baseline present but its vcpu_hours is 0 -> rec vcpu_hours line omits the delta bracket.
    assert "vcpu_hours ~ 900" in text
    assert "900 [" not in text


# ---------- write_family_report ----------


def test_write_family_report_skipped(tmp_path):
    r = _family(skipped_reason="no data in window")
    rep.write_family_report(tmp_path, r, [], [])
    text = (tmp_path / "g5.md").read_text()
    assert "## Skipped" in text
    assert "no data in window" in text


def test_write_family_report_no_recommendation(tmp_path):
    r = _family(best_config=None, best_metrics=None, verdict="unchanged")
    rep.write_family_report(tmp_path, r, [], [])
    text = (tmp_path / "g5.md").read_text()
    assert "## Recommendation" in text
    assert "(none — verdict: unchanged)" in text


def test_write_family_report_baseline_without_cost_lines(tmp_path):
    r = _family(baseline_cost=None, best_config=None, best_metrics=None)
    rep.write_family_report(tmp_path, r, [], [])
    text = (tmp_path / "g5.md").read_text()
    assert "vcpu_hours ~ 800 (compute proxy, not cost)" in text
    assert "node_hours ~" not in text


def test_write_family_report_full(tmp_path):
    r = _family(
        verdict="improved",
        best_config={"g5__g5.4xlarge": {"instance": "g5.4xlarge", "pods": ["def-a", "def-missing"]}},
        best_metrics=_sm(opt_max=0.90, vcpu_hours=700.0),
        baseline_cost=_cost(node_hours=100.0, usd=1000.0),
        rec_cost=_cost(node_hours=80.0, usd=800.0),
        cluster_baseline_metrics=_sm(opt_max=0.60, vcpu_hours=1000.0),
        cluster_rec_metrics=_sm(opt_max=0.75, vcpu_hours=900.0),
        mode="hill",
        configs_evaluated=42,
        elapsed_sec=3.5,
        cache_hit_rate=0.25,
        restarts_run=2,
    )
    rep.write_family_report(tmp_path, r, [], [_entry("def-a")])
    text = (tmp_path / "g5.md").read_text()
    # utilization
    assert "opt_max = 80.0% (cpu 78.0%, mem 80.0%)" in text
    assert "opt_max = 90.0%" in text
    # vcpu_hours labeled compute proxy
    assert "vcpu_hours ~ 800 (compute proxy, not cost)" in text
    assert "(compute proxy, not cost)" in text
    # node-hours (from cost lines) + cost ratio / authoritative comparison
    assert "node_hours ~ 100" in text
    assert "node_hours ~ 80" in text
    assert "node_hours is a size-blind count" in text
    # per-def shape rows: covered def + missing-catalog def
    assert "main vcpu: 16 -> 20" in text
    assert "def-missing (no catalog entry — check eligibility)" in text
    # sections
    assert "## Recommendation (1 sub-nodepool(s))" in text
    assert "## Cluster contribution" in text
    assert "## Convergence" in text
    assert "mode = hill" in text
    assert "## Verdict: improved" in text


# ---------- _classify_rename ----------


def test_classify_rename_true_above_threshold():
    assert rep._classify_rename(16, 64, 20, 64, 10.0) is True


def test_classify_rename_false_within_threshold():
    assert rep._classify_rename(16, 64, 16, 64, 10.0) is False


def test_classify_rename_zero_orig_guards_return_false():
    assert rep._classify_rename(0, 0, 4, 8, 10.0) is False


# ---------- write_family_patch ----------


def test_write_family_patch_skipped(tmp_path):
    r = _family(skipped_reason="no data")
    rep.write_family_patch(tmp_path, r, [], [])
    text = (tmp_path / "g5.patch").read_text()
    assert "# SKIPPED family g5: no data" in text


def test_write_family_patch_no_recommendation(tmp_path):
    r = _family(best_config=None, best_metrics=None, verdict="unchanged")
    rep.write_family_patch(tmp_path, r, [], [])
    text = (tmp_path / "g5.patch").read_text()
    assert "no recommendation (verdict: unchanged)" in text


def test_write_family_patch_full_with_rename_and_missing_entry(tmp_path):
    r = _family(
        verdict="improved",
        best_config={
            "g5__g5.4xlarge": {"instance": "g5.4xlarge", "pods": ["def-a", "def-b", "def-missing"]},
        },
        best_metrics=_sm(),
    )
    catalog = [
        _entry("def-a", orig_main_vcpu=16, new_main_vcpu=20, adj_cpu_pct=25.0),
        _entry("def-b", orig_main_vcpu=16, new_main_vcpu=16, adj_cpu_pct=0.0),
    ]
    rep.write_family_patch(tmp_path, r, [], catalog)
    text = (tmp_path / "g5.patch").read_text()
    assert "# Family: g5" in text
    assert "## New sub-nodepool YAMLs (create):" in text
    assert "modules/nodepools/defs/g5-g5.4xlarge.yaml" in text
    assert "## Per-def changes:" in text
    # def-a is in baseline -> node_fleet from real pool; def-b is not -> "?" default
    assert "node_fleet: g5 -> g5__g5.4xlarge" in text
    assert "node_fleet: ? -> g5__g5.4xlarge" in text
    # def-missing has no catalog entry -> skipped, no per-def line
    assert "arc-runners/defs/def-missing.yaml" not in text
    # rename section only lists the >threshold def
    assert "## Rename required (adjustment > 10.0%):" in text
    assert "def-a: cpu +25.0%" in text
    assert "def-b:" not in text


def test_write_family_patch_no_rename_section_when_all_within_threshold(tmp_path):
    r = _family(
        verdict="improved",
        best_config={"g5__g5.4xlarge": {"instance": "g5.4xlarge", "pods": ["def-a"]}},
        best_metrics=_sm(),
    )
    catalog = [_entry("def-a", orig_main_vcpu=16, new_main_vcpu=16, orig_main_memory_gib=64, new_main_memory_gib=64)]
    rep.write_family_patch(tmp_path, r, [], catalog)
    text = (tmp_path / "g5.patch").read_text()
    assert "## Rename required" not in text


# ---------- _fmt_pp_row / _fmt_vcpu_row ----------


def test_fmt_pp_row():
    assert rep._fmt_pp_row("x", 0.60, 0.75) == "| x | 60.0% | 75.0% | +15.0pp |"


def test_fmt_vcpu_row_base_positive():
    assert rep._fmt_vcpu_row("x", 1000.0, 900.0) == "| x | 1,000 | 900 | -10.0% |"


def test_fmt_vcpu_row_base_zero_is_na():
    assert rep._fmt_vcpu_row("x", 0.0, 5.0) == "| x | 0 | 5 | n/a |"


# ---------- write_global_report ----------


def test_write_global_report_all_row_kinds_no_cluster(tmp_path):
    results = [
        _family(family="skf", skipped_reason="no data", elapsed_sec=1.0),
        _family(family="nof", baseline_metrics=None, best_metrics=None, verdict="unchanged", configs_evaluated=3),
        _family(
            family="okf",
            baseline_metrics=_sm(opt_max=0.60),
            best_config={"okf__i": {"instance": "i", "pods": ["p"]}},
            best_metrics=_sm(opt_max=0.75),
            verdict="improved",
            configs_evaluated=10,
            elapsed_sec=2.0,
        ),
    ]
    rep.write_global_report(tmp_path, results, cluster_validation=None)
    text = (tmp_path / "global.md").read_text()
    assert "# Global search summary" in text
    assert "| skf | n/a | n/a | n/a | skipped (no data) | 0 | 1 |" in text
    assert "| nof | n/a | n/a | n/a | unchanged | 3 | 0 |" in text
    assert "| okf | 60.0% | 75.0% | +15.00 | improved | 10 | 2 |" in text
    assert "Cluster-wide validation" not in text


def test_write_global_report_with_cluster_validation(tmp_path):
    results = [
        _family(
            family="okf",
            baseline_metrics=_sm(opt_max=0.60),
            best_config={"okf__i": {"instance": "i", "pods": ["p"]}},
            best_metrics=_sm(opt_max=0.75),
            verdict="improved",
        ),
    ]
    cluster = ClusterValidationResult(
        baseline_metrics=_sm(opt_max=0.60, opt_cpu=0.55, opt_mem=0.60, cal_cpu=0.50, cal_mem=0.52, vcpu_hours=1000.0),
        recommendation_metrics=_sm(
            opt_max=0.75, opt_cpu=0.70, opt_mem=0.75, cal_cpu=0.65, cal_mem=0.66, vcpu_hours=800.0
        ),
        days=7,
        elapsed_sec=12.0,
        per_family_contrib={},
        baseline_cost=_cost(node_hours=1000.0, usd=10000.0, unpriced_node_hours=100.0, unpriced_types=["p5.48xlarge"]),
        recommendation_cost=_cost(
            node_hours=800.0, usd=8000.0, unpriced_node_hours=40.0, unpriced_types=["p5.48xlarge"]
        ),
    )
    rep.write_global_report(tmp_path, results, cluster_validation=cluster)
    text = (tmp_path / "global.md").read_text()
    assert "## Cluster-wide validation (7d full dataset, all families)" in text
    assert "| opt_max" in text
    assert "vcpu_hours (compute proxy)" in text
    # % saved lines
    assert "% node-hours saved: +20.0%" in text
    assert "% priced-$ saved: +20.0%" in text
    # reserved / unpriced segregated from priced total
    assert "reserved/unpriced node-hours (NOT in priced total): baseline 100, rec 40" in text
    assert "p5.48xlarge" in text


# ---------- runner-fleet render helpers ----------


def test_fmt_price_and_ratio_none_and_value():
    assert runner_rep._fmt_price(8.5) == "$8.500"
    assert runner_rep._fmt_price(None) == "n/a"
    assert runner_rep._fmt_ratio_pct(0.90) == "-10.0%"
    assert runner_rep._fmt_ratio_pct(None) == "n/a"


def test_append_runner_winner_none():
    lines: list[str] = []
    runner_rep._append_runner_winner(lines, "best amd64", None, None)
    assert lines == ["- best amd64: none"]


def test_append_runner_winner_with_and_without_caveat():
    with_lines: list[str] = []
    runner_rep._append_runner_winner(with_lines, "best arm64", _cand("m7g.12xlarge", "arm64"), "gated caveat")
    assert "(gated caveat)" in with_lines[0]

    no_lines: list[str] = []
    runner_rep._append_runner_winner(no_lines, "best amd64", _cand("c7i.48xlarge", "amd64"), None)
    assert "**c7i.48xlarge**" in no_lines[0]
    # no caveat -> line ends at node-hours with no trailing "(...)" tag.
    assert no_lines[0].endswith("node-hours")


# ---------- write_runner_fleet_report ----------


def test_write_runner_fleet_report_full(tmp_path):
    amd = _cand("c7i.48xlarge", "amd64", cost_ratio=0.90)
    arm = _cand("m7g.12xlarge", "arm64", cost_ratio=0.30)
    unpriced = _cand(
        "m8g.24xlarge",
        "arm64",
        blended_price=None,
        blended_cost=None,
        cost_ratio=None,
        region_prices={REGIONS[0]: 2.0, REGIONS[1]: 2.0, REGIONS[2]: None},
    )
    result = _fleet(
        ranked=[amd, arm, unpriced],
        best_amd64=amd,
        best_arm64=arm,
        region_winners=[RegionWinner(region=r, instance_type="c7i.48xlarge", cost=800.0) for r in REGIONS],
        global_winner=amd,
        global_cost=2500.0,
        per_region_optimal_total=2400.0,
        global_penalty_abs=100.0,
        global_penalty_ratio=1.05,
    )
    runner_rep.write_runner_fleet_report(tmp_path, result)
    text = (tmp_path / "runner_fleet.md").read_text()
    assert "# Runner-fleet host optimization (Phase 2.5)" in text
    assert "`c7i-runner` fleet" in text
    # labeled global-winner section: amd64 pick renders vs-baseline and carries NO gated marker
    assert "## Global winner" in text
    assert "- **c7i.48xlarge** (amd64) — -10.0% vs baseline" in text
    assert runner_rep.ARM_GATED_MARKER not in text
    # dual winner: actionable amd64 + gated arm64 with the ARM caveat
    assert "best amd64 (ACTIONABLE today): **c7i.48xlarge**" in text
    assert "best arm64 (GATED): **m7g.12xlarge**" in text
    assert ARM_CAVEAT in text
    # per-region winners + global penalty line, both flagged approximate
    assert f"- {REGIONS[0]}: c7i.48xlarge ($800.00 approx)" in text
    assert "forcing one global instance (c7i.48xlarge) costs" in text
    assert "approx)" in text
    # ranked table header + rows; absolute-$ caveat carries the price-table as-of date
    assert "| instance | arch | slots | node-hours |" in text
    for region in REGIONS:
        assert region in text
    assert "absolute $ are approximate" in text
    _assert_as_of_note(text)
    assert "| m8g.24xlarge | arm64 |" in text
    assert "n/a" in text


def test_write_runner_fleet_report_arm64_global_winner_gated(tmp_path):
    arm = _cand("m7g.12xlarge", "arm64", cost_ratio=0.30)
    amd = _cand("c7i.48xlarge", "amd64", cost_ratio=0.90)
    result = _fleet(
        ranked=[arm, amd],
        best_amd64=amd,
        best_arm64=arm,
        region_winners=[RegionWinner(region=r, instance_type="m7g.12xlarge", cost=300.0) for r in REGIONS],
        global_winner=arm,
        global_cost=900.0,
        per_region_optimal_total=900.0,
        global_penalty_abs=0.0,
        global_penalty_ratio=1.0,
    )
    runner_rep.write_runner_fleet_report(tmp_path, result)
    text = (tmp_path / "runner_fleet.md").read_text()
    marker = runner_rep.ARM_GATED_MARKER
    # labeled global winner line is gated because the winner is arm64
    assert f"- **m7g.12xlarge** (arm64) — -70.0% vs baseline ({marker})" in text
    # every per-region winner line carries the gated marker (all arm64 picks)
    for region in REGIONS:
        assert f"- {region}: m7g.12xlarge ($300.00 approx) ({marker})" in text
    # penalty line carries the marker too
    assert f"(~$0.00 approx) ({marker})" in text


def test_write_runner_fleet_report_empty_winners(tmp_path):
    result = _fleet(
        ranked=[],
        best_amd64=None,
        best_arm64=None,
        region_winners=[],
        global_winner=None,
        global_penalty_ratio=None,
    )
    runner_rep.write_runner_fleet_report(tmp_path, result)
    text = (tmp_path / "runner_fleet.md").read_text()
    report_lines = text.splitlines()
    assert "## Global winner" in report_lines
    assert "- none" in report_lines
    assert "- best amd64 (ACTIONABLE today): none" in text
    assert "- best arm64 (GATED): none" in text
    assert "forcing one global instance" not in text
    assert "## Ranked candidates" in text
