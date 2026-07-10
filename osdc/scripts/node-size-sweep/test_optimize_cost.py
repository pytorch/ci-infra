"""Unit tests for optimize_cost (node-hours and dollar-cost post-processing)."""

from __future__ import annotations

import optimize_cost as oc
import pytest

BH = 300 / 3600  # node-hours per 5-minute bucket


def _prices() -> dict:
    """Bare inner price map; per-region prices differ so blended != us-east-1.

    p5.48xlarge is intentionally absent -> it surfaces as unpriced everywhere.
    """
    return {"c7i.48xlarge": {"us-east-1": 10.0, "us-east-2": 12.0, "us-west-1": 14.0}}


def _sim_two_buckets() -> dict:
    """Two buckets, two pools; "cpu" recurs so per-pool accumulation is exercised."""
    return {
        "per_bucket": [
            (
                0,
                {
                    "cpu": {"node_counts_by_type": {"c7i.48xlarge": 2, "p5.48xlarge": 1}},
                    "gpu": {"node_counts_by_type": {"c7i.48xlarge": 1}},
                },
            ),
            (300, {"cpu": {"node_counts_by_type": {"c7i.48xlarge": 2}}}),
        ]
    }


def test_bucket_hours_matches_simulate():
    assert pytest.approx(BH) == oc.BUCKET_HOURS


def test_node_hours_blended_totals():
    res = oc.node_hours_and_cost(_sim_two_buckets(), prices=_prices())
    assert res["region"] == "blended"
    assert res["node_hours"] == pytest.approx(6 * BH)
    assert res["priced_node_hours"] == pytest.approx(5 * BH)
    assert res["unpriced_node_hours"] == pytest.approx(1 * BH)
    assert res["usd"] == pytest.approx(5 * 12.0 * BH)
    assert res["unpriced_types"] == ["p5.48xlarge"]


def test_node_hours_region_differs_from_blended():
    res = oc.node_hours_and_cost(_sim_two_buckets(), region="us-east-1", prices=_prices())
    assert res["region"] == "us-east-1"
    assert res["usd"] == pytest.approx(5 * 10.0 * BH)
    assert res["node_hours"] == pytest.approx(6 * BH)


def test_node_hours_unknown_region_raises():
    with pytest.raises(ValueError, match="unknown region: eu-west-9"):
        oc.node_hours_and_cost(_sim_two_buckets(), region="eu-west-9", prices=_prices())


def test_node_hours_all_known_regions_accepted():
    for region in (*oc.REGIONS, "blended"):
        res = oc.node_hours_and_cost(_sim_two_buckets(), region=region, prices=_prices())
        assert res["region"] == region


def test_abs_usd_caveat_includes_as_of_date():
    assert oc._AS_OF is not None
    assert f"prices as of {oc._AS_OF}" in oc.ABS_USD_CAVEAT


def test_node_hours_per_pool_breakdown():
    res = oc.node_hours_and_cost(_sim_two_buckets(), prices=_prices())
    cpu = res["per_pool"]["cpu"]
    gpu = res["per_pool"]["gpu"]
    assert cpu["node_hours"] == pytest.approx(5 * BH)
    assert cpu["usd"] == pytest.approx(4 * 12.0 * BH)
    assert cpu["unpriced_node_hours"] == pytest.approx(1 * BH)
    assert cpu["unpriced_types"] == ["p5.48xlarge"]
    assert gpu["node_hours"] == pytest.approx(1 * BH)
    assert gpu["usd"] == pytest.approx(1 * 12.0 * BH)
    assert gpu["unpriced_node_hours"] == pytest.approx(0.0)
    assert gpu["unpriced_types"] == []


def test_none_key_guard_counts_as_unpriced_without_crashing():
    sim = {"per_bucket": [(0, {"cpu": {"node_counts_by_type": {None: 3, "c7i.48xlarge": 1}}})]}
    res = oc.node_hours_and_cost(sim, prices=_prices())
    assert res["priced_node_hours"] == pytest.approx(1 * BH)
    assert res["unpriced_node_hours"] == pytest.approx(3 * BH)
    assert res["node_hours"] == pytest.approx(4 * BH)
    assert res["usd"] == pytest.approx(1 * 12.0 * BH)
    assert res["unpriced_types"] == []
    assert None not in res["unpriced_types"]


def test_missing_node_counts_key_is_empty():
    sim = {"per_bucket": [(0, {"empty": {}})]}
    res = oc.node_hours_and_cost(sim, prices=_prices())
    assert res["node_hours"] == pytest.approx(0.0)
    assert res["per_pool"]["empty"]["node_hours"] == pytest.approx(0.0)


def test_empty_sim_out_is_zeros():
    res = oc.node_hours_and_cost({})
    assert res["node_hours"] == 0.0
    assert res["priced_node_hours"] == 0.0
    assert res["unpriced_node_hours"] == 0.0
    assert res["usd"] == 0.0
    assert res["unpriced_types"] == []
    assert res["per_pool"] == {}


def test_savings_normal_positive_when_reduced():
    baseline = {"node_hours": 100.0, "usd": 1000.0}
    rec = {"node_hours": 80.0, "usd": 800.0}
    s = oc.savings(baseline, rec)
    assert s["pct_node_hours"] == pytest.approx(0.2)
    assert s["pct_usd_priced"] == pytest.approx(0.2)
    assert s["denominator_note"] == oc.DENOM_NOTE


def test_savings_zero_baseline_node_hours_and_usd_return_none():
    s = oc.savings({"node_hours": 0.0, "usd": 0.0}, {"node_hours": 5.0, "usd": 5.0})
    assert s["pct_node_hours"] is None
    assert s["pct_usd_priced"] is None


def test_savings_zero_usd_only_returns_none_for_usd():
    s = oc.savings({"node_hours": 10.0, "usd": 0.0}, {"node_hours": 8.0, "usd": 0.0})
    assert s["pct_node_hours"] == pytest.approx(0.2)
    assert s["pct_usd_priced"] is None


def test_family_baseline_cost_lines_none_is_empty():
    assert oc.family_baseline_cost_lines(None) == []


def test_family_baseline_cost_lines_priced_only():
    cost = {"node_hours": 100.0, "usd": 1000.0, "unpriced_node_hours": 0.0, "unpriced_types": []}
    lines = oc.family_baseline_cost_lines(cost)
    assert len(lines) == 2
    assert oc.NODE_HOURS_CAVEAT in lines[0]
    assert oc.ABS_USD_CAVEAT in lines[1]
    assert "100" in lines[0]
    assert "1,000" in lines[1]


def test_family_baseline_cost_lines_with_unpriced():
    cost = {"node_hours": 100.0, "usd": 1000.0, "unpriced_node_hours": 5.0, "unpriced_types": ["p5.48xlarge"]}
    lines = oc.family_baseline_cost_lines(cost)
    assert len(lines) == 3
    assert "reserved/unpriced" in lines[2]
    assert "p5.48xlarge" in lines[2]
    assert "excluded from $" in lines[2]


def test_family_rec_cost_lines_none_rec_is_empty():
    assert oc.family_rec_cost_lines({"node_hours": 1.0}, None) == []


def test_family_rec_cost_lines_with_baseline_shows_change_sign():
    baseline = {"node_hours": 100.0, "usd": 1000.0, "unpriced_node_hours": 0.0, "unpriced_types": []}
    rec = {"node_hours": 80.0, "usd": 800.0, "unpriced_node_hours": 0.0, "unpriced_types": []}
    lines = oc.family_rec_cost_lines(baseline, rec)
    assert len(lines) == 4
    assert "-20 vs baseline" in lines[0]
    assert "-20.0%" in lines[0]
    assert oc.NODE_HOURS_CAVEAT in lines[0]
    assert "priced-$ change: -20.0%" in lines[1]
    assert oc.ABS_USD_CAVEAT in lines[1]
    assert "approx cost ~ $800/window" in lines[2]
    assert oc.ABS_USD_CAVEAT in lines[2]
    assert "node_hours is a size-blind count" in lines[3]


def test_family_rec_cost_lines_with_baseline_abs_matches_baseline_format():
    baseline = {"node_hours": 100.0, "usd": 1000.0, "unpriced_node_hours": 0.0, "unpriced_types": []}
    rec = {"node_hours": 80.0, "usd": 800.0, "unpriced_node_hours": 0.0, "unpriced_types": []}
    rec_abs = oc.family_rec_cost_lines(baseline, rec)[2]
    base_abs = oc.family_baseline_cost_lines(rec)[1]
    assert rec_abs == base_abs


def test_family_rec_cost_lines_na_when_baseline_zero():
    baseline = {"node_hours": 0.0, "usd": 0.0, "unpriced_node_hours": 0.0, "unpriced_types": []}
    rec = {"node_hours": 5.0, "usd": 50.0, "unpriced_node_hours": 0.0, "unpriced_types": []}
    lines = oc.family_rec_cost_lines(baseline, rec)
    assert "n/a" in lines[0]
    assert "priced-$ change: n/a" in lines[1]


def test_family_rec_cost_lines_no_baseline_and_reserved():
    rec = {"node_hours": 50.0, "usd": 500.0, "unpriced_node_hours": 5.0, "unpriced_types": ["p5.48xlarge"]}
    lines = oc.family_rec_cost_lines(None, rec)
    assert len(lines) == 3
    assert oc.NODE_HOURS_CAVEAT in lines[0]
    assert oc.ABS_USD_CAVEAT in lines[1]
    assert "reserved/unpriced" in lines[2]
    assert "p5.48xlarge" in lines[2]


def test_cluster_cost_lines_none_guards():
    rec = {"node_hours": 1.0, "usd": 1.0, "unpriced_node_hours": 0.0, "unpriced_types": []}
    assert oc.cluster_cost_lines(None, rec) == []
    assert oc.cluster_cost_lines(rec, None) == []


def test_cluster_cost_lines_populated_saved_positive():
    baseline = {"node_hours": 100.0, "usd": 1000.0, "unpriced_node_hours": 10.0, "unpriced_types": ["p5.48xlarge"]}
    rec = {"node_hours": 80.0, "usd": 800.0, "unpriced_node_hours": 4.0, "unpriced_types": ["p6-b200.48xlarge"]}
    lines = oc.cluster_cost_lines(baseline, rec)
    assert lines[0] == ""
    assert lines[1].startswith("### Cost")
    assert "% node-hours saved: +20.0%" in lines[2]
    assert oc.NODE_HOURS_CAVEAT in lines[2]
    assert "% priced-$ saved: +20.0%" in lines[3]
    assert oc.DENOM_NOTE in lines[3]
    assert "reserved/unpriced node-hours" in lines[4]
    assert "p5.48xlarge" in lines[4]
    assert "p6-b200.48xlarge" in lines[4]
    assert oc.ABS_USD_CAVEAT in lines[5]


def test_cluster_cost_lines_na_and_no_types():
    baseline = {"node_hours": 0.0, "usd": 0.0, "unpriced_node_hours": 0.0, "unpriced_types": []}
    rec = {"node_hours": 5.0, "usd": 50.0, "unpriced_node_hours": 0.0, "unpriced_types": []}
    lines = oc.cluster_cost_lines(baseline, rec)
    assert "% node-hours saved: n/a" in lines[2]
    assert "% priced-$ saved: n/a" in lines[3]
    assert "reserved/unpriced node-hours" in lines[4]
    assert "(types:" not in lines[4]


def test_cluster_cost_lines_from_real_sim():
    base_cost = oc.node_hours_and_cost(_sim_two_buckets(), prices=_prices())
    rec_sim = {"per_bucket": [(0, {"cpu": {"node_counts_by_type": {"c7i.48xlarge": 1}}})]}
    rec_cost = oc.node_hours_and_cost(rec_sim, prices=_prices())
    lines = oc.cluster_cost_lines(base_cost, rec_cost)
    assert any("node-hours saved" in ln for ln in lines)
    assert any("p5.48xlarge" in ln for ln in lines)
