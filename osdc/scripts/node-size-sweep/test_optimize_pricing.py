"""Unit tests for optimize_pricing (the node-size optimizer dollar-cost layer)."""

from __future__ import annotations

import json

import optimize_pricing as op
import pytest


def _ranking_prices() -> dict:
    """Largest-instance-only fixture with hand-picked unit costs for ranking assertions.

    All three regions share one price per type, so blended == that price.
    Only the family representatives are priced, which pins the candidate set.
    """

    def entry(v: float) -> dict:
        return {"us-east-1": v, "us-east-2": v, "us-west-1": v}

    return {
        "prices": {
            "c7i.48xlarge": entry(9.6),  # 192/384  -> vcpu 0.05,  gib 0.025
            "m6i.32xlarge": entry(7.68),  # 128/512  -> vcpu 0.06,  gib 0.015
            "r7i.48xlarge": entry(13.44),  # 192/1536 -> vcpu 0.07,  gib 0.00875
            "c7a.48xlarge": entry(8.064),  # 192/384  -> vcpu 0.042, gib 0.021 (us-west-1 excluded)
            "r7a.48xlarge": entry(11.52),  # 192/1536 -> vcpu 0.06,  gib 0.0075 (us-west-1 excluded)
            "m8g.48xlarge": entry(15.36),  # 192/768  -> vcpu 0.08,  gib 0.02  (arm64)
            "t4g.2xlarge": entry(0.24),  # 8/32     -> vcpu 0.03,  gib 0.0075 (arm64)
            "g5.48xlarge": entry(16.0),  # GPU family — must be filtered out
        }
    }


def _edge_prices() -> dict:
    """Small fixture exercising null handling and region-absent lookups."""
    return {
        "c7i.48xlarge": {"us-east-1": 9.6, "us-east-2": 9.6, "us-west-1": 9.6},
        "c7a.48xlarge": {"us-east-1": 8.0, "us-east-2": 8.0, "us-west-1": None},
        "r7a.48xlarge": {"us-east-1": None, "us-east-2": None, "us-west-1": None},
        "m6i.24xlarge": {"us-east-1": 4.6, "us-east-2": 4.6},  # us-west-1 key absent
    }


def test_regions_constant():
    assert op.REGIONS == ("us-east-1", "us-east-2", "us-west-1")


def test_load_prices_default_real_json():
    data = op.load_prices()
    assert isinstance(data["prices"], dict)
    assert data["prices"]


def test_load_prices_custom_path(tmp_path):
    payload = {"prices": {"c7i.2xlarge": {"us-east-1": 1.0}}}
    p = tmp_path / "custom_prices.json"
    p.write_text(json.dumps(payload))
    assert op.load_prices(p)["prices"]["c7i.2xlarge"]["us-east-1"] == 1.0


def test_load_prices_returns_isolated_copy():
    first = op.load_prices()
    first["prices"]["c7i.48xlarge"]["us-east-1"] = -999.0
    second = op.load_prices()
    assert second["prices"]["c7i.48xlarge"]["us-east-1"] != -999.0


def test_prices_map_accepts_full_table_and_bare_map():
    full = _ranking_prices()
    bare = full["prices"]
    assert op._prices_map(full) is bare
    assert op._prices_map(bare) is bare


def test_hourly_price_present_and_missing():
    prices = _edge_prices()
    assert op.hourly_price("c7i.48xlarge", "us-east-1", prices) == 9.6
    assert op.hourly_price("c7a.48xlarge", "us-west-1", prices) is None  # explicit null
    assert op.hourly_price("m6i.24xlarge", "us-west-1", prices) is None  # region key absent
    assert op.hourly_price("does.not-exist", "us-east-1", prices) is None


def test_hourly_price_default_prices_real_json():
    val = op.hourly_price("c7i.48xlarge", "us-east-1")
    assert isinstance(val, float)
    assert val > 0


def test_blended_price_skips_nulls_and_handles_all_null():
    prices = _edge_prices()
    assert op.blended_price("c7i.48xlarge", prices) == pytest.approx(9.6)
    assert op.blended_price("c7a.48xlarge", prices) == pytest.approx(8.0)  # null region skipped
    assert op.blended_price("m6i.24xlarge", prices) == pytest.approx(4.6)  # absent region skipped
    assert op.blended_price("r7a.48xlarge", prices) is None  # all null
    assert op.blended_price("does.not-exist", prices) is None


def test_region_available_family_exclusions():
    assert op.region_available("c7a.48xlarge", "us-west-1") is False
    assert op.region_available("r7a.24xlarge", "us-west-1") is False
    assert op.region_available("g5.48xlarge", "us-west-1") is False
    assert op.region_available("c7i.48xlarge", "us-west-1") is True
    assert op.region_available("c7a.48xlarge", "us-east-1") is True
    assert op.region_available("m8g.24xlarge", "us-east-2") is True


def test_family_representative_prefers_largest_then_name():
    prices = {
        "c7i.24xlarge": {"us-east-1": 4.0},
        "c7i.48xlarge": {"us-east-1": 8.0},
        "c7i.metal-48xl": {"us-east-1": 8.0},
    }
    assert op._family_representative("c7i", prices) == "c7i.48xlarge"


def test_family_representative_none_when_unpriced():
    assert op._family_representative("c7i", {}) is None


def test_family_cost_efficiency_math_and_skips_unpriced():
    eff = op.family_cost_efficiency(_ranking_prices())
    assert eff["c7i"]["usd_per_vcpu"] == pytest.approx(0.05)
    assert eff["c7i"]["usd_per_gib"] == pytest.approx(0.025)
    assert eff["r7i"]["usd_per_gib"] == pytest.approx(0.00875)
    assert eff["t4g"]["usd_per_vcpu"] == pytest.approx(0.03)
    # A real CPU family absent from the fixture has no representative and is omitted.
    assert "m7g" not in eff


def test_select_candidate_families_amd64_all_regions():
    got = op.select_candidate_families(["amd64"], require_all_regions=True, prices=_ranking_prices())
    assert got == ["c7i", "m6i", "r7i"]  # c7a, r7a dropped (us-west-1 excluded)


def test_select_candidate_families_amd64_no_region_requirement():
    got = op.select_candidate_families(["amd64"], require_all_regions=False, prices=_ranking_prices())
    assert got == ["c7a", "c7i", "m6i", "r7a", "r7i"]


def test_select_candidate_families_mixed_arch():
    got = op.select_candidate_families(["amd64", "arm64"], require_all_regions=True, prices=_ranking_prices())
    assert got == ["c7i", "m6i", "r7i", "t4g"]


def test_select_candidate_families_drops_gpu_family():
    got = op.select_candidate_families(["amd64"], require_all_regions=False, prices=_ranking_prices())
    assert "g5" not in got


def test_cost_of_node_counts_region_split():
    prices = _edge_prices()
    counts = {"c7i.48xlarge": 2.0, "p5.48xlarge": 3.0, "c7a.48xlarge": 1.0}
    res = op.cost_of_node_counts(counts, "us-west-1", prices)
    assert res["priced_usd_per_hr"] == pytest.approx(19.2)
    assert res["priced_node_count"] == pytest.approx(2.0)
    assert res["unpriced_node_count"] == pytest.approx(4.0)
    assert res["unpriced_types"] == ["c7a.48xlarge", "p5.48xlarge"]


def test_cost_of_node_counts_blended():
    prices = _edge_prices()
    counts = {"c7i.48xlarge": 2.0, "p5.48xlarge": 3.0, "c7a.48xlarge": 1.0}
    res = op.cost_of_node_counts(counts, "blended", prices)
    assert res["priced_usd_per_hr"] == pytest.approx(27.2)
    assert res["priced_node_count"] == pytest.approx(3.0)
    assert res["unpriced_node_count"] == pytest.approx(3.0)
    assert res["unpriced_types"] == ["p5.48xlarge"]


def test_cost_of_node_counts_default_prices():
    res = op.cost_of_node_counts({"p5.48xlarge": 1.0}, "blended")
    assert res["unpriced_node_count"] == pytest.approx(1.0)
    assert res["unpriced_types"] == ["p5.48xlarge"]


def test_real_json_schema_and_values():
    data = op.load_prices()
    for key in ("_as_of", "_sources", "_notes", "_unconfirmed", "prices"):
        assert key in data
    assert data["_as_of"]
    assert isinstance(data["_sources"], list)
    assert data["_sources"]
    assert isinstance(data["_unconfirmed"], list)
    for itype, by_region in data["prices"].items():
        assert "." in itype
        for region, price in by_region.items():
            assert region in op.REGIONS
            assert price is None or (isinstance(price, float) and price > 0)


def test_real_json_reserved_families_absent():
    prices = op.load_prices()["prices"]
    assert "p5.48xlarge" not in prices
    assert "p6-b200.48xlarge" not in prices
