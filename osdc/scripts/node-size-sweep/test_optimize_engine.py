"""Unit tests for optimize_engine (packing-sim + config-search engine).

Pure logic (config enumeration, neighbor moves, metric extraction, ranking,
cache keys) is tested directly. Anything needing a real packing sim uses the
smallest possible real ClusterModel + synthetic jobs — no network, no cluster.
"""

from __future__ import annotations

import logging
import random

import optimize_engine as eng
import pytest
from optimize_catalog import EligibleEntry
from optimize_storage import SimCache, SimMetrics
from sim_nodes import FleetSpec, Job

VCPU_HOUR_PER_BUCKET_MC = 1 / 1000.0 * (300 / 3600.0)  # millicore-bucket -> vcpu-hours


def _entry(
    def_label: str,
    instance: str,
    *,
    slot_cpu_m: int = 4000,
    slot_mem_mi: int = 8192,
    slot_gpu: int = 0,
    orig_gpu: int = 0,
) -> EligibleEntry:
    return EligibleEntry(
        def_label=def_label,
        instance=instance,
        n=2,
        slot_cpu_m=slot_cpu_m,
        slot_mem_mi=slot_mem_mi,
        slot_gpu=slot_gpu,
        orig_cpu_m=slot_cpu_m,
        orig_mem_mi=slot_mem_mi,
        orig_gpu=orig_gpu,
        new_main_vcpu=4,
        orig_main_vcpu=4,
        new_main_memory_gib=8,
        orig_main_memory_gib=8,
        adj_cpu_pct=0.0,
        adj_mem_pct=0.0,
    )


def _job(label: str, pool: str = "orig", cpu_m: int = 4000, mem_mi: int = 8192, gpu: int = 0) -> Job:
    return Job(label=label, pool=pool, cpu_m=cpu_m, mem_mi=mem_mi, gpu=gpu, start_bucket=0, end_bucket=600)


def _pool_sums(
    *,
    workload_cpu: int = 0,
    workload_mem: int = 0,
    alloc_cpu: int = 0,
    alloc_mem: int = 0,
    ds_cpu: int = 0,
    ds_mem: int = 0,
    cal_used_cpu: int = 0,
    cal_alloc_cpu: int = 0,
    cal_used_mem: int = 0,
    cal_alloc_mem: int = 0,
) -> dict:
    return {
        "workload_cpu_m": workload_cpu,
        "workload_mem_mi": workload_mem,
        "alloc_cpu_m_raw": alloc_cpu,
        "alloc_mem_mi_raw": alloc_mem,
        "ds_cpu_m": ds_cpu,
        "ds_mem_mi": ds_mem,
        "cpu_used_m": cal_used_cpu,
        "cpu_alloc_m": cal_alloc_cpu,
        "mem_used_mi": cal_used_mem,
        "mem_alloc_mi": cal_alloc_mem,
    }


def _sim_flags() -> dict:
    return {
        "seed": 42,
        "empty_ttl_buckets": 1,
        "placeholder_max_age": 2,
        "warmup_default": 0,
        "warmup_gpu": 0,
        "warmup_baremetal": 0,
        "placeholders_enabled": False,
        "daemonsets_in_metric": True,
        "phantom_pods_enabled": False,
    }


def _runner_fleet() -> FleetSpec:
    return FleetSpec(name="c7i-runner", is_gpu=False, instances=("c7i.2xlarge",))


def _rng() -> random.Random:
    return random.Random(0)  # noqa: S311  # deterministic test fixture, not crypto


# ---------- ids, canonicalization, hashing ----------


def test_sub_nodepool_id():
    assert eng.sub_nodepool_id("c7i", "c7i.4xlarge") == "c7i__c7i.4xlarge"


def test_canonical_config_sorts_pods_and_subpools():
    a = {"z": {"instance": "i", "pods": ["b", "a"]}, "a": {"instance": "j", "pods": ["c"]}}
    b = {"a": {"instance": "j", "pods": ["c"]}, "z": {"instance": "i", "pods": ["a", "b"]}}
    assert eng.canonical_config(a) == eng.canonical_config(b)
    assert '"pods":["a","b"]' in eng.canonical_config(a)


def test_config_cache_key_stable_and_sha256_len():
    cfg1 = {"z": {"instance": "i", "pods": ["b", "a"]}}
    cfg2 = {"z": {"instance": "i", "pods": ["a", "b"]}}
    k1 = eng.config_cache_key("c7i", cfg1, {"x": 1}, "sha", {"src": "1"})
    k2 = eng.config_cache_key("c7i", cfg2, {"x": 1}, "sha", {"src": "1"})
    assert k1 == k2
    assert len(k1) == 64


def test_config_cache_key_changes_with_flags():
    cfg = {"z": {"instance": "i", "pods": ["a"]}}
    assert eng.config_cache_key("c7i", cfg, {"x": 1}, "sha", {}) != eng.config_cache_key(
        "c7i", cfg, {"x": 2}, "sha", {}
    )


# ---------- catalog helpers ----------


def test_build_family_catalog_and_lookups():
    entries = [_entry("a", "c7i.2xlarge"), _entry("a", "c7i.4xlarge"), _entry("b", "c7i.4xlarge")]
    catalog = eng.build_family_catalog(entries)
    assert set(catalog) == {("a", "c7i.2xlarge"), ("a", "c7i.4xlarge"), ("b", "c7i.4xlarge")}
    assert eng.instances_in_catalog(entries) == ["c7i.2xlarge", "c7i.4xlarge"]
    assert eng.eligible_instances_for_def(entries, "a") == ["c7i.2xlarge", "c7i.4xlarge"]
    assert eng.eligible_instances_for_def(entries, "b") == ["c7i.4xlarge"]
    assert eng.instances_in_catalog([]) == []


def test_is_config_feasible_true_and_false():
    catalog = eng.build_family_catalog([_entry("a", "c7i.4xlarge")])
    assert eng.is_config_feasible({"s": {"instance": "c7i.4xlarge", "pods": ["a"]}}, catalog) is True
    assert eng.is_config_feasible({"s": {"instance": "c7i.2xlarge", "pods": ["a"]}}, catalog) is False
    assert eng.is_config_feasible({}, {}) is True


# ---------- baseline feasibility + construction ----------


def _real_daemonsets() -> list:
    from daemonset_overhead import discover_daemonsets
    from sim_nodes import REPO_ROOT

    return discover_daemonsets(REPO_ROOT)


def test_is_baseline_feasible_fits():
    defs = [{"name": "a", "vcpu": 4, "memory_gib": 8, "gpu": 0}]
    config = {"c7i": {"instance": "c7i.8xlarge", "pods": ["a"]}}
    fleets = {"c7i": FleetSpec(name="c7i", is_gpu=False, instances=("c7i.8xlarge",))}
    assert eng.is_baseline_feasible(config, defs, _real_daemonsets(), fleets) is True


def test_is_baseline_feasible_missing_def_returns_false():
    config = {"c7i": {"instance": "c7i.8xlarge", "pods": ["ghost"]}}
    fleets = {"c7i": FleetSpec(name="c7i", is_gpu=False, instances=("c7i.8xlarge",))}
    assert eng.is_baseline_feasible(config, [{"name": "a", "vcpu": 1, "memory_gib": 1, "gpu": 0}], [], fleets) is False


def test_is_baseline_feasible_too_big_returns_false():
    defs = [{"name": "big", "vcpu": 9999, "memory_gib": 99999, "gpu": 0}]
    config = {"c7i": {"instance": "c7i.8xlarge", "pods": ["big"]}}
    fleets = {"c7i": FleetSpec(name="c7i", is_gpu=False, instances=("c7i.8xlarge",))}
    assert eng.is_baseline_feasible(config, defs, _real_daemonsets(), fleets) is False


def test_is_baseline_feasible_fleet_missing_falls_back_to_reference_instance():
    defs = [{"name": "a", "vcpu": 4, "memory_gib": 8, "gpu": 0}]
    config = {"unknown-pool": {"instance": "c7i.8xlarge", "pods": ["a"]}}
    assert eng.is_baseline_feasible(config, defs, _real_daemonsets(), {}) is True


def test_is_baseline_feasible_unknown_reference_instance_skips_and_fails():
    defs = [{"name": "a", "vcpu": 4, "memory_gib": 8, "gpu": 0}]
    config = {"unknown-pool": {"instance": "not-a-real-instance", "pods": ["a"]}}
    assert eng.is_baseline_feasible(config, defs, _real_daemonsets(), {}) is False


def test_baseline_config_single_prod_instance():
    defs = [
        {"name": "a", "instance_type": "c7i.8xlarge", "node_fleet": None},
        {"name": "b", "instance_type": "c7i.8xlarge", "node_fleet": None},
    ]
    cfg = eng.baseline_config("c7i", defs, [_entry("a", "c7i.8xlarge")])
    assert cfg == {"c7i": {"instance": "c7i.8xlarge", "pods": ["a", "b"]}}


def test_baseline_config_multi_instance_picks_largest_vcpu():
    defs = [
        {"name": "a", "instance_type": "c7i.4xlarge", "node_fleet": None},
        {"name": "b", "instance_type": "c7i.24xlarge", "node_fleet": None},
    ]
    cfg = eng.baseline_config("c7i", defs, [])
    assert cfg["c7i"]["instance"] == "c7i.24xlarge"


def test_baseline_config_node_fleet_override_splits_pools():
    defs = [
        {"name": "a", "instance_type": "c7i.8xlarge", "node_fleet": None},
        {"name": "b", "instance_type": "c7i.8xlarge", "node_fleet": "custom-pool"},
    ]
    cfg = eng.baseline_config("c7i", defs, [])
    assert set(cfg) == {"c7i", "custom-pool"}
    assert cfg["custom-pool"]["pods"] == ["b"]


def test_baseline_config_unknown_instances_fall_back_to_catalog_last():
    defs = [
        {"name": "a", "instance_type": "fake.big", "node_fleet": None},
        {"name": "b", "instance_type": "fake.small", "node_fleet": None},
    ]
    cfg = eng.baseline_config("fake", defs, [_entry("a", "c7i.4xlarge"), _entry("b", "c7i.8xlarge")])
    assert cfg["fake"]["instance"] == "c7i.8xlarge"


def test_baseline_config_no_defs_raises():
    with pytest.raises(ValueError, match="no defs"):
        eng.baseline_config("c7i", [], [])


def test_baseline_config_unknown_instances_empty_catalog_raises():
    defs = [
        {"name": "a", "instance_type": "fake.big", "node_fleet": None},
        {"name": "b", "instance_type": "fake.small", "node_fleet": None},
    ]
    with pytest.raises(ValueError, match="catalog empty"):
        eng.baseline_config("fake", defs, [])


# ---------- partition enumeration ----------


@pytest.mark.parametrize(
    ("items", "count"),
    [([], 1), (["a"], 1), (["a", "b"], 2), (["a", "b", "c"], 5), (["a", "b", "c", "d"], 15)],
)
def test_iter_set_partitions_bell_numbers(items, count):
    parts = list(eng._iter_set_partitions(list(items)))
    assert len(parts) == count
    for p in parts:
        flat = sorted(x for block in p for x in block)
        assert flat == sorted(items)


def test_enumerate_configs_for_partition_merges_duplicate_instance():
    catalog = eng.build_family_catalog([_entry("a", "c7i.4xlarge"), _entry("b", "c7i.4xlarge")])
    per_def = {"a": ["c7i.4xlarge"], "b": ["c7i.4xlarge"]}
    cfgs = list(eng._enumerate_configs_for_partition("c7i", [["a"], ["b"]], catalog, per_def))
    assert [eng.canonical_config(c) for c in cfgs] == [
        '{"c7i__c7i.4xlarge":{"instance":"c7i.4xlarge","pods":["a","b"]}}'
    ]


def test_enumerate_configs_for_partition_prunes_empty_intersection():
    catalog = eng.build_family_catalog([_entry("a", "c7i.4xlarge"), _entry("b", "c7i.2xlarge")])
    per_def = {"a": ["c7i.4xlarge"], "b": ["c7i.2xlarge"]}
    assert list(eng._enumerate_configs_for_partition("c7i", [["a", "b"]], catalog, per_def)) == []


def test_enumerate_configs_for_partition_skips_infeasible_instance():
    catalog = eng.build_family_catalog([_entry("a", "c7i.4xlarge")])
    per_def = {"a": ["c7i.4xlarge", "c7i.24xlarge"]}
    cfgs = list(eng._enumerate_configs_for_partition("c7i", [["a"]], catalog, per_def))
    assert [eng.canonical_config(c) for c in cfgs] == ['{"c7i__c7i.4xlarge":{"instance":"c7i.4xlarge","pods":["a"]}}']


def test_enumerate_configs_for_partition_dedups_canonical_collisions(monkeypatch):
    # The partition invariant (disjoint blocks) means distinct instance choices
    # never collide in practice, so the intra-partition `seen` guard is defensive.
    # Force every config to the same canonical key to prove the guard drops the
    # duplicate: two eligible instances -> two raw configs -> one survivor.
    catalog = eng.build_family_catalog([_entry("a", "c7i.2xlarge"), _entry("a", "c7i.4xlarge")])
    per_def = {"a": ["c7i.2xlarge", "c7i.4xlarge"]}
    monkeypatch.setattr(eng, "canonical_config", lambda _cfg: "COLLIDE")
    cfgs = list(eng._enumerate_configs_for_partition("c7i", [["a"]], catalog, per_def))
    assert len(cfgs) == 1


def test_enumerate_feasible_configs_counts_and_gating():
    entries = [_entry("a", "c7i.2xlarge"), _entry("a", "c7i.4xlarge"), _entry("b", "c7i.4xlarge")]
    defs = [{"name": "a"}, {"name": "b"}]
    configs, capped = eng.enumerate_feasible_configs("c7i", defs, entries)
    assert capped is False
    keys = {eng.canonical_config(c) for c in configs}
    assert keys == {
        '{"c7i__c7i.4xlarge":{"instance":"c7i.4xlarge","pods":["a","b"]}}',
        '{"c7i__c7i.2xlarge":{"instance":"c7i.2xlarge","pods":["a"]},'
        '"c7i__c7i.4xlarge":{"instance":"c7i.4xlarge","pods":["b"]}}',
    }


def test_enumerate_feasible_configs_limit_caps():
    entries = [_entry("a", "c7i.2xlarge"), _entry("a", "c7i.4xlarge"), _entry("b", "c7i.4xlarge")]
    defs = [{"name": "a"}, {"name": "b"}]
    configs, capped = eng.enumerate_feasible_configs("c7i", defs, entries, limit=1)
    assert capped is True
    assert len(configs) == 2  # returns after len(out) > limit, so limit+1 emitted


def test_enumerate_feasible_configs_empty_defs():
    configs, capped = eng.enumerate_feasible_configs("c7i", [], [])
    assert capped is False
    assert [eng.canonical_config(c) for c in configs] == ["{}"]


# ---------- hill-climb neighbor moves ----------


def _abc_setup() -> tuple[list[EligibleEntry], list[dict], dict[str, list[str]]]:
    insts = ["c7i.2xlarge", "c7i.4xlarge"]
    entries = [_entry(d, i) for d in ("a", "b", "c") for i in insts]
    defs = [{"name": "a"}, {"name": "b"}, {"name": "c"}]
    per_def = {"a": insts, "b": insts, "c": insts}
    return entries, defs, per_def


def test_config_copy_is_deep_enough():
    cfg = {"s": {"instance": "i", "pods": ["a", "b"]}}
    cp = eng.config_copy(cfg)
    cp["s"]["pods"].append("z")
    assert cfg["s"]["pods"] == ["a", "b"]
    assert eng._config_copy is eng.config_copy


def test_neighbors_move_pod_to_existing_and_new_singleton():
    _, _, per_def = _abc_setup()
    cfg = {
        "c7i__c7i.2xlarge": {"instance": "c7i.2xlarge", "pods": ["a", "b"]},
        "c7i__c7i.4xlarge": {"instance": "c7i.4xlarge", "pods": ["c"]},
    }
    out = [eng.canonical_config(c) for c in eng._neighbors_move_pod("c7i", cfg, per_def)]
    assert out  # move-pod produces neighbors
    # moving the lone 'c' out of the 4xl pool deletes that empty pool
    assert any('"pods":["a","b","c"]' in c and "c7i__c7i.4xlarge" not in c for c in out)


def test_neighbors_move_pod_skips_ineligible_target_pool():
    # 'a' is eligible only for 2xl, so it can never move into the 4xl pool.
    per_def = {"a": ["c7i.2xlarge"], "b": ["c7i.4xlarge"], "c": ["c7i.2xlarge", "c7i.4xlarge"]}
    cfg = {
        "c7i__c7i.2xlarge": {"instance": "c7i.2xlarge", "pods": ["a", "c"]},
        "c7i__c7i.4xlarge": {"instance": "c7i.4xlarge", "pods": ["b"]},
    }
    out = [eng.canonical_config(c) for c in eng._neighbors_move_pod("c7i", cfg, per_def)]
    # 'a' must never appear alone in the 4xl pool (it's ineligible there)
    assert not any('"c7i__c7i.4xlarge":{"instance":"c7i.4xlarge","pods":["a"' in c for c in out)
    assert out


def test_neighbors_merge_and_no_common_skipped():
    _, _, per_def = _abc_setup()
    cfg = {
        "c7i__c7i.2xlarge": {"instance": "c7i.2xlarge", "pods": ["a"]},
        "c7i__c7i.4xlarge": {"instance": "c7i.4xlarge", "pods": ["b"]},
    }
    merged = list(eng._neighbors_merge("c7i", cfg, per_def))
    assert len(merged) == 2  # merge to 2xl or to 4xl
    # a def with no shared eligible instance cannot be merged
    per_def_split = {"a": ["c7i.2xlarge"], "b": ["c7i.4xlarge"]}
    assert list(eng._neighbors_merge("c7i", cfg, per_def_split)) == []


def test_neighbors_merge_single_pool_yields_nothing():
    _, _, per_def = _abc_setup()
    cfg = {"c7i__c7i.2xlarge": {"instance": "c7i.2xlarge", "pods": ["a"]}}
    assert list(eng._neighbors_merge("c7i", cfg, per_def)) == []


def test_neighbors_merge_collision_extends_existing_pool():
    # Three pools, all defs eligible everywhere. Merging the 2xl and 4xl pools
    # onto the 8xl instance collides with the existing 8xl pool -> extend branch.
    per_def = {n: ["c7i.2xlarge", "c7i.4xlarge", "c7i.8xlarge"] for n in "abc"}
    cfg = {
        "c7i__c7i.2xlarge": {"instance": "c7i.2xlarge", "pods": ["a"]},
        "c7i__c7i.4xlarge": {"instance": "c7i.4xlarge", "pods": ["b"]},
        "c7i__c7i.8xlarge": {"instance": "c7i.8xlarge", "pods": ["c"]},
    }
    out = [eng.canonical_config(c) for c in eng._neighbors_merge("c7i", cfg, per_def)]
    assert '{"c7i__c7i.8xlarge":{"instance":"c7i.8xlarge","pods":["a","b","c"]}}' in out


def test_neighbors_split_peels_subset_and_skips_singletons():
    _, _, per_def = _abc_setup()
    cfg = {
        "c7i__c7i.2xlarge": {"instance": "c7i.2xlarge", "pods": ["a", "b"]},
        "c7i__c7i.4xlarge": {"instance": "c7i.4xlarge", "pods": ["c"]},
    }
    split = list(eng._neighbors_split("c7i", cfg, per_def))
    assert split  # peels a/b onto the 4xl pool
    # single-pod source pools cannot be split
    cfg1 = {"c7i__c7i.2xlarge": {"instance": "c7i.2xlarge", "pods": ["a"]}}
    assert list(eng._neighbors_split("c7i", cfg1, per_def)) == []


def test_neighbors_split_subset_with_no_common_instance_is_skipped():
    # a=2xl-only, b=4xl-only, c=both. Any subset containing both a and b has an
    # empty eligible-instance intersection and must be skipped, while subsets
    # like {a}, {c}, {a,c} still yield splits.
    per_def = {"a": ["c7i.2xlarge"], "b": ["c7i.4xlarge"], "c": ["c7i.2xlarge", "c7i.4xlarge"]}
    cfg = {"c7i__c7i.4xlarge": {"instance": "c7i.4xlarge", "pods": ["a", "b", "c"]}}
    out = [eng.canonical_config(c) for c in eng._neighbors_split("c7i", cfg, per_def)]
    assert all("no-such" not in c for c in out)
    # {a,b} onto 2xl would need b eligible for 2xl (it isn't) -> never produced
    assert not any('"c7i__c7i.2xlarge":{"instance":"c7i.2xlarge","pods":["a","b"]}' in c for c in out)
    assert out  # the feasible subsets still split


def test_neighbors_change_instance_and_collision_merges():
    _, _, per_def = _abc_setup()
    cfg = {
        "c7i__c7i.2xlarge": {"instance": "c7i.2xlarge", "pods": ["a"]},
        "c7i__c7i.4xlarge": {"instance": "c7i.4xlarge", "pods": ["b"]},
    }
    out = [eng.canonical_config(c) for c in eng._neighbors_change_instance("c7i", cfg, per_def)]
    # changing one pool's instance to the other's collapses into a single pool
    assert '{"c7i__c7i.4xlarge":{"instance":"c7i.4xlarge","pods":["a","b"]}}' in out
    assert '{"c7i__c7i.2xlarge":{"instance":"c7i.2xlarge","pods":["a","b"]}}' in out


def test_neighbors_change_instance_no_common_skipped():
    per_def = {"a": ["c7i.2xlarge"], "b": ["c7i.4xlarge"]}
    cfg = {"c7i__c7i.2xlarge": {"instance": "c7i.2xlarge", "pods": ["a", "b"]}}
    assert list(eng._neighbors_change_instance("c7i", cfg, per_def)) == []


def test_enumerate_neighbors_dedups_and_gates_feasibility():
    entries, defs, _ = _abc_setup()
    cfg = {"c7i__c7i.2xlarge": {"instance": "c7i.2xlarge", "pods": ["a", "b"]}}
    out = eng.enumerate_neighbors("c7i", cfg, defs, entries)
    keys = {eng.canonical_config(c) for c in out}
    assert len(keys) == len(out)  # no duplicates
    assert eng.canonical_config(cfg) not in keys  # source excluded
    for c in out:
        assert eng.is_config_feasible(c, eng.build_family_catalog(entries))


def test_enumerate_neighbors_skips_empty_and_infeasible_from_generators(monkeypatch):
    entries = [_entry("a", "c7i.4xlarge")]
    defs = [{"name": "a"}]
    cfg = {"c7i__c7i.4xlarge": {"instance": "c7i.4xlarge", "pods": ["a"]}}

    def _bad_gen(*_args, **_kwargs):
        yield {}  # falsy config -> skipped by the `if not cfg` guard
        yield {"x": {"instance": "not-in-catalog", "pods": ["a"]}}  # infeasible -> skipped

    monkeypatch.setattr(eng, "_neighbors_move_pod", _bad_gen)
    monkeypatch.setattr(eng, "_neighbors_merge", lambda *a, **k: iter(()))
    monkeypatch.setattr(eng, "_neighbors_split", lambda *a, **k: iter(()))
    monkeypatch.setattr(eng, "_neighbors_change_instance", lambda *a, **k: iter(()))
    assert eng.enumerate_neighbors("c7i", cfg, defs, entries) == []


def test_random_feasible_config_groups_and_skips_ineligible():
    entries = [_entry("a", "c7i.4xlarge")]
    defs = [{"name": "a"}, {"name": "noelig"}]
    cfg = eng.random_feasible_config("c7i", defs, entries, _rng())
    assert eng.canonical_config(cfg) == '{"c7i__c7i.4xlarge":{"instance":"c7i.4xlarge","pods":["a"]}}'


def test_random_feasible_config_raises_on_infeasible(monkeypatch):
    entries = [_entry("a", "c7i.4xlarge")]
    defs = [{"name": "a"}]
    monkeypatch.setattr(eng, "is_config_feasible", lambda *a, **k: False)
    with pytest.raises(RuntimeError, match="infeasible config"):
        eng.random_feasible_config("c7i", defs, entries, _rng())


# ---------- rebuild_jobs_for_config ----------


def test_rebuild_jobs_drops_runner_and_nonfamily_uses_catalog_slots():
    from sim_load import RUNNER_POD_LABEL

    catalog = eng.build_family_catalog([_entry("a", "c7i.4xlarge", slot_cpu_m=5000, slot_mem_mi=9000, slot_gpu=0)])
    config = {"c7i__c7i.4xlarge": {"instance": "c7i.4xlarge", "pods": ["a"]}}
    jobs = [
        _job("a", cpu_m=1, mem_mi=1),
        _job(RUNNER_POD_LABEL, pool="c7i-runner"),
        _job("other-family", cpu_m=1, mem_mi=1),
    ]
    out = eng.rebuild_jobs_for_config("c7i", config, jobs, catalog, {"a"})
    assert len(out) == 1
    assert out[0].label == "a"
    assert out[0].pool == "c7i__c7i.4xlarge"
    assert (out[0].cpu_m, out[0].mem_mi) == (5000, 9000)


def test_rebuild_jobs_unassigned_and_catalog_miss_dropped():
    catalog = eng.build_family_catalog([_entry("a", "c7i.4xlarge")])
    # 'a' is a family def but the config assigns it to an instance absent from catalog
    config = {"c7i__c7i.24xlarge": {"instance": "c7i.24xlarge", "pods": ["a"]}}
    out = eng.rebuild_jobs_for_config("c7i", config, [_job("a")], catalog, {"a"})
    assert out == []  # catalog.get((a, 24xlarge)) is None
    # 'b' is a family def with no assignment in the config at all
    config2 = {"c7i__c7i.4xlarge": {"instance": "c7i.4xlarge", "pods": ["a"]}}
    out2 = eng.rebuild_jobs_for_config("c7i", config2, [_job("b")], catalog, {"a", "b"})
    assert out2 == []


def test_rebuild_jobs_baseline_uses_original_shapes():
    catalog = eng.build_family_catalog([_entry("a", "c7i.8xlarge", slot_cpu_m=5000, slot_mem_mi=9000)])
    config = {"c7i": {"instance": "c7i.8xlarge", "pods": ["a"]}}
    baseline_defs = [{"name": "a", "vcpu": 4, "memory_gib": 8, "gpu": 0}]
    out = eng.rebuild_jobs_for_config("c7i", config, [_job("a")], catalog, {"a"}, baseline_defs=baseline_defs)
    assert len(out) == 1
    # baseline shape comes from def_totals, NOT the catalog slot (5000/9000)
    from optimize_config import def_totals

    exp_cpu, exp_mem, exp_gpu, _, _ = def_totals(baseline_defs[0])
    assert (out[0].cpu_m, out[0].mem_mi, out[0].gpu) == (exp_cpu, exp_mem, exp_gpu)
    assert out[0].pool == "c7i"


def test_rebuild_jobs_baseline_missing_shape_dropped():
    catalog = eng.build_family_catalog([_entry("a", "c7i.8xlarge")])
    config = {"c7i": {"instance": "c7i.8xlarge", "pods": ["a"]}}
    # baseline_defs lacks 'a', so its shape is unknown and the job is dropped
    out = eng.rebuild_jobs_for_config("c7i", config, [_job("a")], catalog, {"a"}, baseline_defs=[])
    assert out == []


# ---------- fleet override / cluster fleets ----------


def test_build_fleets_override_preserves_runner_and_detects_gpu():
    config = {
        "c7i__c7i.8xlarge": {"instance": "c7i.8xlarge", "pods": ["a"]},
        "g5__g5.4xlarge": {"instance": "g5.4xlarge", "pods": ["g"]},
    }
    fleets = eng.build_fleets_override(config, _runner_fleet())
    assert fleets["c7i-runner"] == _runner_fleet()
    assert fleets["c7i__c7i.8xlarge"].is_gpu is False
    assert fleets["c7i__c7i.8xlarge"].instances == ("c7i.8xlarge",)
    assert fleets["g5__g5.4xlarge"].is_gpu is True


def test_build_cluster_fleets_extra_only_improved_with_config():
    class R:
        def __init__(self, verdict, cfg):
            self.verdict = verdict
            self.best_config = cfg

    results = [
        R("improved", {"c7i__c7i.8xlarge": {"instance": "c7i.8xlarge", "pods": ["a"]}}),
        R("no_improvement", {"x": {"instance": "c7i.8xlarge", "pods": ["b"]}}),
        R("improved", None),
    ]
    extra = eng.build_cluster_fleets_extra(results)
    assert set(extra) == {"c7i__c7i.8xlarge"}
    assert extra["c7i__c7i.8xlarge"].instances == ("c7i.8xlarge",)


def test_build_cluster_fleets_extra_missing_attrs_skipped():
    assert eng.build_cluster_fleets_extra([object()]) == {}


# ---------- metric extraction ----------


def test_extract_family_metrics_math_and_name_matching():
    config = {"c7i__c7i.4xlarge": {"instance": "c7i.4xlarge", "pods": ["a"]}}
    good = _pool_sums(
        workload_cpu=1000,
        workload_mem=2000,
        alloc_cpu=4000,
        alloc_mem=8000,
        cal_used_cpu=1000,
        cal_alloc_cpu=4000,
        cal_used_mem=4000,
        cal_alloc_mem=8000,
    )
    sim_out = {
        "per_bucket": [
            (
                0,
                {
                    "c7i__c7i.4xlarge": good,
                    "c7i__zero": _pool_sums(alloc_cpu=0, alloc_mem=0),  # prefix match but zero-alloc -> skipped
                    "unrelated": _pool_sums(workload_cpu=9999, alloc_cpu=9999),  # neither key nor prefix -> ignored
                },
            ),
            (300, {"c7i__c7i.4xlarge": good}),
        ]
    }
    m = eng._extract_family_metrics(sim_out, "c7i", config, daemonsets=["ignored"])
    assert m.opt_cpu == pytest.approx(0.25)
    assert m.opt_mem == pytest.approx(0.25)
    assert m.opt_max == pytest.approx(0.25)
    assert m.cal_cpu == pytest.approx(0.25)
    assert m.cal_mem == pytest.approx(0.5)
    assert m.vcpu_hours == pytest.approx((4000 + 4000) * VCPU_HOUR_PER_BUCKET_MC)


def test_extract_family_metrics_matches_config_key_without_prefix():
    config = {"c7i": {"instance": "c7i.8xlarge", "pods": ["a"]}}
    sim_out = {"per_bucket": [(0, {"c7i": _pool_sums(workload_cpu=500, alloc_cpu=2000)})]}
    m = eng._extract_family_metrics(sim_out, "c7i", config)
    assert m.opt_cpu == pytest.approx(0.25)


def test_extract_family_metrics_all_zero_yields_zero_ratios():
    config = {"c7i__x": {"instance": "c7i.4xlarge", "pods": ["a"]}}
    sim_out = {"per_bucket": [(0, {"c7i__x": _pool_sums(alloc_cpu=0, alloc_mem=0)})]}
    m = eng._extract_family_metrics(sim_out, "c7i", config)
    assert (m.opt_cpu, m.opt_mem, m.opt_max, m.cal_cpu, m.cal_mem, m.vcpu_hours) == (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)


def test_accumulate_pool_sums_cluster_vs_filtered():
    sim_out = {
        "per_bucket": [
            (
                0,
                {
                    "p1": _pool_sums(workload_cpu=1000, alloc_cpu=4000),
                    "p2": _pool_sums(workload_cpu=2000, alloc_cpu=4000),
                },
            )
        ]
    }
    cluster = eng.extract_cluster_metrics(sim_out)
    assert cluster.opt_cpu == pytest.approx(3000 / 8000)
    assert cluster.vcpu_hours == pytest.approx(8000 * VCPU_HOUR_PER_BUCKET_MC)
    contrib = eng.extract_family_contribution_metrics(sim_out, {"p1"})
    assert contrib.opt_cpu == pytest.approx(1000 / 4000)
    assert contrib.vcpu_hours == pytest.approx(4000 * VCPU_HOUR_PER_BUCKET_MC)


def test_accumulate_pool_sums_filtered_skips_zero_alloc_pool():
    sim_out = {
        "per_bucket": [
            (
                0,
                {
                    "p1": _pool_sums(workload_cpu=1000, alloc_cpu=4000),
                    "p2": _pool_sums(
                        workload_cpu=9999, alloc_cpu=0, alloc_mem=0
                    ),  # in filter but zero-alloc -> skipped
                },
            )
        ]
    }
    contrib = eng.extract_family_contribution_metrics(sim_out, {"p1", "p2"})
    assert contrib.opt_cpu == pytest.approx(1000 / 4000)  # p2's 9999 workload excluded


# ---------- apply recommendations ----------


def test_apply_recommendations_rewrites_matched_and_passes_through():
    jobs = [_job("a", pool="orig", cpu_m=1), _job("keep", pool="kpool", cpu_m=7)]
    overrides = {"a": {"pool": "newpool", "cpu_m": 5000, "mem_mi": 9000, "gpu": 1}}
    out = eng.apply_recommendations_to_jobs(jobs, overrides)
    by_label = {j.label: j for j in out}
    assert (by_label["a"].pool, by_label["a"].cpu_m, by_label["a"].mem_mi, by_label["a"].gpu) == (
        "newpool",
        5000,
        9000,
        1,
    )
    assert (by_label["keep"].pool, by_label["keep"].cpu_m) == ("kpool", 7)


# ---------- ranking ----------


def _metrics(opt_max: float, vcpu_hours: float) -> SimMetrics:
    return SimMetrics(opt_max=opt_max, opt_cpu=0.0, opt_mem=0.0, cal_cpu=0.0, cal_mem=0.0, vcpu_hours=vcpu_hours)


def test_rank_key_higher_opt_max_wins():
    assert eng.rank_key(_metrics(0.5, 10)) < eng.rank_key(_metrics(0.6, 10))


def test_rank_key_tie_broken_by_lower_vcpu_hours():
    lo = _metrics(0.6, 50)
    hi = _metrics(0.6, 999)
    assert eng.rank_key(lo) > eng.rank_key(hi)
    assert max([hi, lo], key=eng.rank_key) is lo


# ---------- real-sim wrappers ----------


def _rec_setup() -> tuple[dict, dict, list[Job], set[str]]:
    catalog = eng.build_family_catalog([_entry("a", "c7i.8xlarge"), _entry("b", "c7i.8xlarge")])
    config = {"c7i__c7i.8xlarge": {"instance": "c7i.8xlarge", "pods": ["a", "b"]}}
    jobs = [_job("a"), _job("b"), _job("runner-pod", pool="c7i-runner", cpu_m=750, mem_mi=1024)]
    return catalog, config, jobs, {"a", "b"}


def test_run_sim_for_config_recommendation_path():
    catalog, config, jobs, fam = _rec_setup()
    m = eng.run_sim_for_config("c7i", config, jobs, catalog, fam, _runner_fleet(), _sim_flags())
    assert not m.empty
    assert m.opt_max > 0.0
    assert m.vcpu_hours > 0.0
    assert m.elapsed_s >= 0.0


def test_run_sim_for_config_baseline_path():
    catalog = eng.build_family_catalog([_entry("a", "c7i.8xlarge")])
    config = {"c7i": {"instance": "c7i.8xlarge", "pods": ["a"]}}
    jobs = [_job("a"), _job("runner-pod", pool="c7i-runner", cpu_m=750, mem_mi=1024)]
    baseline_defs = [{"name": "a", "vcpu": 4, "memory_gib": 8, "gpu": 0}]
    m = eng.run_sim_for_config(
        "c7i", config, jobs, catalog, {"a"}, _runner_fleet(), _sim_flags(), baseline_defs=baseline_defs
    )
    assert not m.empty
    assert m.opt_max > 0.0


def test_run_sim_for_config_empty_returns_empty_metrics():
    catalog, config, _, fam = _rec_setup()
    m = eng.run_sim_for_config("c7i", config, [], catalog, fam, _runner_fleet(), _sim_flags())
    assert m.empty is True
    assert m.opt_max == 0.0


def test_cost_for_config_returns_cost_dict():
    catalog, config, jobs, fam = _rec_setup()
    cost = eng.cost_for_config("c7i", config, jobs, catalog, fam, _runner_fleet(), _sim_flags())
    assert cost is not None
    assert set(cost) >= {"node_hours", "usd", "per_pool", "region"}
    assert cost["node_hours"] > 0.0


def test_cost_for_config_empty_returns_none():
    catalog, config, _, fam = _rec_setup()
    assert eng.cost_for_config("c7i", config, [], catalog, fam, _runner_fleet(), _sim_flags()) is None


def test_run_cluster_sim_real_model_and_empty_raises():
    jobs = [_job("a", pool="c7i"), _job("runner-pod", pool="c7i-runner", cpu_m=750, mem_mi=1024)]
    out = eng.run_cluster_sim(jobs, None, _sim_flags())
    assert out["per_bucket"]
    assert eng.extract_cluster_metrics(out).opt_max > 0.0
    with pytest.raises(ValueError, match="empty jobs"):
        eng.run_cluster_sim([], None, _sim_flags())


# ---------- caching glue ----------


def test_cached_sim_miss_then_hit_and_baseline_distinct_key(tmp_path):
    # Config keyed by a REAL fleet name so both the recommendation path
    # (fleets_override) and the baseline path (real ClusterModel) resolve.
    catalog = eng.build_family_catalog([_entry("a", "c7i.8xlarge")])
    config = {"c7i": {"instance": "c7i.8xlarge", "pods": ["a"]}}
    jobs = [_job("a"), _job("runner-pod", pool="c7i-runner", cpu_m=750, mem_mi=1024)]
    fam = {"a"}
    cache = SimCache(tmp_path / "cache.db")
    log = logging.getLogger("test_optimize_engine")
    m1, hit1 = eng.cached_sim("c7i", config, jobs, catalog, fam, _runner_fleet(), _sim_flags(), "sha", {}, cache, log)
    assert hit1 is False
    m2, hit2 = eng.cached_sim("c7i", config, jobs, catalog, fam, _runner_fleet(), _sim_flags(), "sha", {}, cache, log)
    assert hit2 is True
    assert m1.opt_max == m2.opt_max
    # a baseline sim for the SAME config must not collide with the recommendation entry
    baseline_defs = [{"name": "a", "vcpu": 4, "memory_gib": 8, "gpu": 0}]
    _, hit_bl = eng.cached_sim(
        "c7i",
        config,
        jobs,
        catalog,
        fam,
        _runner_fleet(),
        _sim_flags(),
        "sha",
        {},
        cache,
        log,
        baseline_defs=baseline_defs,
    )
    assert hit_bl is False


# ---------- result dataclass ----------


def test_family_result_defaults():
    r = eng.FamilyResult(
        family="c7i",
        baseline_config={},
        baseline_metrics=None,
        best_config=None,
        best_metrics=None,
        verdict="skipped",
    )
    assert r.per_def_shapes == {}
    assert r.configs_evaluated == 0
    assert r.cache_hit_rate == 0.0
    assert r.baseline_cost is None
    assert r.cluster_rec_metrics is None
