"""Unit tests for optimize_runner_fleet (closed-form runner-pod host optimizer)."""

from __future__ import annotations

import optimize_runner_fleet as rf
import pytest
from instance_specs import INSTANCE_SPECS
from sim_nodes import ClusterModel, Job

RUNNER_CPU_M = 750
RUNNER_MEM_MI = 1024


def _runner(sb: int, eb: int, pool: str = rf.RUNNER_POD_POOL) -> Job:
    return Job(
        label="runner-pod", pool=pool, cpu_m=RUNNER_CPU_M, mem_mi=RUNNER_MEM_MI, gpu=0, start_bucket=sb, end_bucket=eb
    )


def _model() -> ClusterModel:
    return ClusterModel()


def _controlled_prices() -> dict:
    """All-regions-equal prices pinning the candidate set to c7i (amd64) + m7g (arm64).

    m7g is priced far below c7i so the arm64 winner is unambiguous. c7a is priced
    in all three regions to exercise the us-west-1 region exclusion path.
    """

    def entry(v: float) -> dict:
        return {"us-east-1": v, "us-east-2": v, "us-west-1": v}

    return {
        "prices": {
            "c7i.2xlarge": entry(3.6),
            "c7i.8xlarge": entry(6.0),
            "c7i.48xlarge": entry(8.568),
            "m7g.2xlarge": entry(0.33),
            "m7g.12xlarge": entry(2.0),
            "c7a.48xlarge": entry(8.0),
        }
    }


# ---------- slots math + memory-never-binds proof ----------


def test_slots_math_known_instances():
    m = _model()
    assert rf.runner_slots(m, rf.RUNNER_POD_POOL, "c7i.2xlarge", RUNNER_CPU_M, RUNNER_MEM_MI) == 9
    assert rf.runner_slots(m, rf.RUNNER_POD_POOL, "c7i.8xlarge", RUNNER_CPU_M, RUNNER_MEM_MI) == 41
    assert rf.runner_slots(m, rf.RUNNER_POD_POOL, "c7i.48xlarge", RUNNER_CPU_M, RUNNER_MEM_MI) == 254


def test_memory_never_binds_sample():
    m = _model()
    for itype in ("c7i.2xlarge", "c7i.48xlarge", "m8g.48xlarge", "r5.8xlarge", "m7g.2xlarge"):
        cpu_slots, mem_slots = rf._slot_counts(m, rf.RUNNER_POD_POOL, itype, RUNNER_CPU_M, RUNNER_MEM_MI)
        assert mem_slots >= cpu_slots


def test_memory_never_binds_arithmetic_proof():
    m = _model()
    for itype in ("c7i.2xlarge", "c7i.48xlarge", "m6i.24xlarge"):
        alloc = m.allocatable(rf.RUNNER_POD_POOL, itype)
        cpu_slots = alloc.cpu_m // RUNNER_CPU_M
        mem_slots = alloc.mem_mi // RUNNER_MEM_MI
        assert mem_slots >= cpu_slots


def test_slot_counts_memory_binds_when_pod_is_memory_heavy():
    m = _model()
    cpu_slots, mem_slots = rf._slot_counts(m, rf.RUNNER_POD_POOL, "c7i.2xlarge", cpu_m=100, mem_mi=100000)
    assert mem_slots < cpu_slots


def test_slot_counts_zero_guards():
    m = _model()
    assert rf._slot_counts(m, rf.RUNNER_POD_POOL, "c7i.2xlarge", cpu_m=0, mem_mi=1024)[0] == 0
    assert rf._slot_counts(m, rf.RUNNER_POD_POOL, "c7i.2xlarge", cpu_m=750, mem_mi=0)[1] == 0


def test_runner_slots_takes_min_binding_constraint():
    m = _model()
    alloc = m.allocatable(rf.RUNNER_POD_POOL, "c7i.2xlarge")
    cpu_slots = alloc.cpu_m // RUNNER_CPU_M
    mem_slots = alloc.mem_mi // 8192
    assert mem_slots < cpu_slots
    assert rf.runner_slots(m, rf.RUNNER_POD_POOL, "c7i.2xlarge", RUNNER_CPU_M, 8192) == mem_slots


def test_runner_slots_zero_guards():
    m = _model()
    assert rf.runner_slots(m, rf.RUNNER_POD_POOL, "c7i.2xlarge", cpu_m=0, mem_mi=RUNNER_MEM_MI) == 0
    assert rf.runner_slots(m, rf.RUNNER_POD_POOL, "c7i.2xlarge", cpu_m=RUNNER_CPU_M, mem_mi=0) == 0


def test_evaluate_candidate_zero_cpu_does_not_crash():
    m = _model()
    got = rf._evaluate_candidate(
        "c7i.2xlarge",
        concurrent=[10],
        model=m,
        runner_pool=rf.RUNNER_POD_POOL,
        runner_cpu_m=0,
        runner_mem_mi=RUNNER_MEM_MI,
        window=1,
        proactive_floor_pods=0,
        prices=_controlled_prices(),
        baseline_cost=1.0,
    )
    assert got is None


# ---------- concurrency timeline ----------


def test_timeline_half_open_intervals():
    jobs = [_runner(0, 300), _runner(0, 600), _runner(300, 600)]
    assert rf.build_concurrency_timeline(jobs, rf.RUNNER_POD_POOL) == [2, 2, 0]


def test_timeline_only_counts_runner_pods():
    jobs = [_runner(0, 300), _runner(0, 300, pool="c7i")]
    assert rf.build_concurrency_timeline(jobs, rf.RUNNER_POD_POOL) == [1, 0]


def test_timeline_falls_back_to_all_jobs_when_no_runner_pods():
    jobs = [_runner(0, 300, pool="c7i"), _runner(0, 600, pool="c7i")]
    assert rf.build_concurrency_timeline(jobs, rf.RUNNER_POD_POOL) == [2, 1, 0]


def test_timeline_empty():
    assert rf.build_concurrency_timeline([], rf.RUNNER_POD_POOL) == []


def test_timeline_skips_zero_length_intervals():
    jobs = [_runner(300, 300), _runner(0, 300)]
    assert rf.build_concurrency_timeline(jobs, rf.RUNNER_POD_POOL) == [1, 0]


# ---------- sliding-window-max lag ----------


def test_sliding_window_max_lookback():
    assert rf._sliding_window_max([1, 3, 2, 0, 0], 3) == [1, 3, 3, 3, 2]


def test_sliding_window_max_window_one_is_identity():
    assert rf._sliding_window_max([2, 1, 3], 1) == [2, 1, 3]


def test_sliding_window_max_window_larger_than_series():
    assert rf._sliding_window_max([1, 5, 2], 10) == [1, 5, 5]


def test_consolidation_window_is_36_buckets():
    assert rf.CONSOLIDATION_WINDOW_BUCKETS == 36


# ---------- node-hours closed form ----------


def test_node_hours_basic():
    # 3 buckets at concurrency [2,1,0], 10 slots, window 3.
    # nodes_raw = [1,1,0]; look-back max -> [1,1,1]; node_hours = 3 * 300/3600.
    assert rf.node_hours_for_slots([2, 1, 0], slots=10, window=3) == pytest.approx(3 * 300 / 3600)


def test_node_hours_lag_extends_after_spike():
    # spike of 30 needs 3 nodes; the 3h look-back holds 3 nodes for later buckets.
    got = rf.node_hours_for_slots([30, 0, 0], slots=10, window=3)
    assert got == pytest.approx(3 * 3 * 300 / 3600)


def test_node_hours_slots_below_one_returns_zero():
    assert rf.node_hours_for_slots([5, 5], slots=0, window=3) == 0.0


def test_node_hours_empty_timeline():
    assert rf.node_hours_for_slots([], slots=10, window=3) == 0.0


def test_node_hours_proactive_floor():
    # 20-pod floor at 10 slots => 2 nodes minimum even when demand is 0/1 node.
    got = rf.node_hours_for_slots([1, 0], slots=10, window=1, proactive_floor_pods=20)
    assert got == pytest.approx(2 * 2 * 300 / 3600)


# ---------- candidate selection ----------


def test_candidate_families_union_per_arch():
    prices = _controlled_prices()
    fams = rf._candidate_families(("amd64", "arm64"), prices)
    assert "c7i" in fams
    assert "m7g" in fams


def test_candidate_instances_excludes_burstable_and_gpu():
    instances = rf._candidate_instances(("amd64", "arm64"), None, exclude_burstable=True)
    for itype in instances:
        assert INSTANCE_SPECS[itype]["gpu"] == 0
        assert itype.split(".")[0] not in rf.BURSTABLE_FAMILIES


def test_candidate_instances_arch_filter():
    prices = _controlled_prices()
    amd_only = rf._candidate_instances(("amd64",), prices, exclude_burstable=True)
    assert all(INSTANCE_SPECS[i]["arch"] == "amd64" for i in amd_only)
    assert all(i.split(".")[0] != "m7g" for i in amd_only)


def test_candidate_instances_skips_unpriced():
    prices = _controlled_prices()
    instances = rf._candidate_instances(("amd64", "arm64"), prices, exclude_burstable=True)
    for itype in instances:
        assert rf.pricing.blended_price(itype, prices) is not None


# ---------- candidate evaluation + region filtering ----------


def test_evaluate_candidate_region_exclusion():
    m = _model()
    prices = _controlled_prices()
    got = rf._evaluate_candidate(
        "c7a.48xlarge",
        concurrent=[10, 10],
        model=m,
        runner_pool=rf.RUNNER_POD_POOL,
        runner_cpu_m=RUNNER_CPU_M,
        runner_mem_mi=RUNNER_MEM_MI,
        window=1,
        proactive_floor_pods=0,
        prices=prices,
        baseline_cost=100.0,
    )
    assert got is not None
    assert got.region_prices["us-west-1"] is None
    assert got.region_costs["us-west-1"] is None
    assert got.region_prices["us-east-1"] == 8.0


def test_evaluate_candidate_slots_below_one_returns_none():
    m = _model()
    got = rf._evaluate_candidate(
        "c7i.2xlarge",
        concurrent=[1],
        model=m,
        runner_pool=rf.RUNNER_POD_POOL,
        runner_cpu_m=10_000_000,
        runner_mem_mi=RUNNER_MEM_MI,
        window=1,
        proactive_floor_pods=0,
        prices=_controlled_prices(),
        baseline_cost=1.0,
    )
    assert got is None


def test_evaluate_candidate_cost_ratio_none_when_baseline_zero():
    m = _model()
    got = rf._evaluate_candidate(
        "c7i.2xlarge",
        concurrent=[10],
        model=m,
        runner_pool=rf.RUNNER_POD_POOL,
        runner_cpu_m=RUNNER_CPU_M,
        runner_mem_mi=RUNNER_MEM_MI,
        window=1,
        proactive_floor_pods=0,
        prices=_controlled_prices(),
        baseline_cost=0.0,
    )
    assert got is not None
    assert got.cost_ratio is None


# ---------- runner shape resolution ----------


def test_resolve_runner_shape_from_jobs():
    jobs = [_runner(0, 300)]
    assert rf._resolve_runner_shape(jobs, rf.RUNNER_POD_POOL) == (RUNNER_CPU_M, RUNNER_MEM_MI)


def test_resolve_runner_shape_fallback_when_no_runner_pods():
    jobs = [_runner(0, 300, pool="c7i")]
    cpu_m, mem_mi = rf._resolve_runner_shape(jobs, rf.RUNNER_POD_POOL)
    assert cpu_m > 0
    assert mem_mi > 0


# ---------- end-to-end optimize_runner_fleet ----------


def _flat_workload(concurrency: int, buckets: int) -> list[Job]:
    return [_runner(0, buckets * 300) for _ in range(concurrency)]


def test_optimize_dual_winner_and_ranking():
    jobs = _flat_workload(concurrency=100, buckets=10)
    res = rf.optimize_runner_fleet(jobs, ("amd64", "arm64"), prices=_controlled_prices(), model=_model())
    assert res.peak_concurrency == 100
    assert res.best_amd64 is not None
    assert res.best_amd64.arch == "amd64"
    assert res.best_arm64 is not None
    assert res.best_arm64.arch == "arm64"
    # m7g priced far below c7i -> arm64 wins outright.
    assert res.global_winner.arch == "arm64"
    assert res.global_winner is res.ranked[0]
    # ranked strictly ascending by blended cost.
    costs = [c.blended_cost for c in res.ranked]
    assert costs == sorted(costs)


def test_optimize_cost_ratio_vs_baseline():
    jobs = _flat_workload(concurrency=100, buckets=10)
    res = rf.optimize_runner_fleet(jobs, ("amd64", "arm64"), prices=_controlled_prices(), model=_model())
    assert res.baseline_instance == "c7i.48xlarge"
    assert res.baseline_slots == 254
    assert res.baseline_blended_cost is not None
    for c in res.ranked:
        assert c.cost_ratio == pytest.approx(c.blended_cost / res.baseline_blended_cost)


def test_optimize_region_winners_and_penalty():
    jobs = _flat_workload(concurrency=100, buckets=10)
    res = rf.optimize_runner_fleet(jobs, ("amd64", "arm64"), prices=_controlled_prices(), model=_model())
    assert {w.region for w in res.region_winners} == set(rf.pricing.REGIONS)
    assert res.global_cost is not None
    assert res.per_region_optimal_total is not None
    # global (single-instance) cost can never beat the per-region optimum.
    assert res.global_penalty_abs >= -1e-9
    assert res.global_penalty_ratio >= 1.0 - 1e-9


def test_optimize_single_size_family_does_not_crash():
    # r7g has exactly one size (r7g.16xlarge); it must evaluate without error.
    prices = {
        "prices": {
            "c7i.48xlarge": {"us-east-1": 8.568, "us-east-2": 8.568, "us-west-1": 10.68},
            "r7g.16xlarge": {"us-east-1": 3.55, "us-east-2": 3.55, "us-west-1": 3.55},
        }
    }
    jobs = _flat_workload(concurrency=50, buckets=5)
    res = rf.optimize_runner_fleet(jobs, ("arm64",), prices=prices, model=_model())
    assert any(c.instance_type == "r7g.16xlarge" for c in res.ranked)


def test_optimize_empty_job_set():
    res = rf.optimize_runner_fleet([], ("amd64", "arm64"), prices=_controlled_prices(), model=_model())
    assert res.peak_concurrency == 0
    assert res.total_buckets == 0
    assert res.ranked == []
    assert res.best_amd64 is None
    assert res.best_arm64 is None
    assert res.region_winners == []
    assert res.global_winner is None


def test_optimize_default_arch_and_window():
    jobs = _flat_workload(concurrency=20, buckets=3)
    res = rf.optimize_runner_fleet(jobs, prices=_controlled_prices(), model=_model())
    assert res.arch_allowlist == ("amd64", "arm64")
    assert res.window_buckets == 36


# ---------- region winner helper edge cases ----------


def test_region_winners_empty_ranked():
    winners, winner, gcost, _opt, _pabs, _pratio = rf._region_winners_and_penalty([])
    assert winners == []
    assert winner is None
    assert gcost is None


def test_region_winners_missing_region_cost():
    c = rf.CandidateResult(
        instance_type="c7a.48xlarge",
        family="c7a",
        arch="amd64",
        slots=254,
        node_hours=1.0,
        blended_price=8.0,
        blended_cost=8.0,
        region_prices={"us-east-1": 8.0, "us-east-2": 8.0, "us-west-1": None},
        region_costs={"us-east-1": 8.0, "us-east-2": 8.0, "us-west-1": None},
        cost_ratio=1.0,
    )
    _winners, winner, gcost, _opt, pabs, _pratio = rf._region_winners_and_penalty([c])
    # us-west-1 has no priced candidate -> penalty cannot be computed.
    assert winner is c
    assert gcost is None
    assert pabs is None


# ---------- differing per-region winners + rank-key tiebreak ----------


def _differing_region_prices() -> dict:
    """c7i cheapest in us-east-{1,2}; m7i cheapest in us-west-1.

    Same-size amd64 families available in all three regions, so the per-region
    optimum picks a DIFFERENT family than the single blended-cheapest global one,
    forcing a non-zero global-vs-per-region penalty.
    """
    return {
        "prices": {
            "c7i.8xlarge": {"us-east-1": 3.0, "us-east-2": 3.0, "us-west-1": 9.0},
            "m7i.8xlarge": {"us-east-1": 8.0, "us-east-2": 8.0, "us-west-1": 4.0},
        }
    }


def test_optimize_region_winners_differ_and_penalty_positive():
    jobs = _flat_workload(concurrency=100, buckets=10)
    res = rf.optimize_runner_fleet(jobs, ("amd64",), prices=_differing_region_prices(), model=_model())
    winners = {w.region: w.instance_type for w in res.region_winners}
    assert winners["us-east-1"] == "c7i.8xlarge"
    assert winners["us-east-2"] == "c7i.8xlarge"
    assert winners["us-west-1"] == "m7i.8xlarge"
    assert len({w.instance_type for w in res.region_winners}) == 2
    assert res.global_penalty_abs > 0.0
    assert res.global_penalty_ratio > 1.0


def _ranking_candidate(itype: str, blended_cost: float, node_hours: float) -> rf.CandidateResult:
    return rf.CandidateResult(
        instance_type=itype,
        family=itype.split(".")[0],
        arch="amd64",
        slots=100,
        node_hours=node_hours,
        blended_price=blended_cost / node_hours,
        blended_cost=blended_cost,
        region_prices={},
        region_costs={},
        cost_ratio=None,
    )


def test_rank_key_tiebreak_prefers_fewer_node_hours():
    high = _ranking_candidate("c7i.8xlarge", blended_cost=10.0, node_hours=8.0)
    low = _ranking_candidate("m7i.8xlarge", blended_cost=10.0, node_hours=5.0)
    ranked = sorted([high, low], key=rf._rank_key)
    assert ranked[0] is low
    assert ranked[1] is high
