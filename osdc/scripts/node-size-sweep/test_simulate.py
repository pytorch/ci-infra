"""Unit tests for the packing simulator's per-bucket output.

Focused on the additive ``node_counts_by_type`` field: presence on every
present pool, consistency with the measured raw allocatable, correct
per-instance-type breakdown when a pool holds mixed sizes, and preservation
of the existing per-pool metric keys and the ``(t, per_pool)`` tuple shape.
"""

from __future__ import annotations

import sys
from pathlib import Path

from sim_nodes import ClusterModel, FleetSpec, Job, Node, Placeholder
from simulate import (
    _mark_empty_if_needed,
    _place_free,
    _place_phantom,
    _pool_class,
    _preempt_placeholder,
    _warmup_buckets,
    main,
    simulate,
)

SAMPLE_CSV = Path(__file__).resolve().parent / "sample.csv"

EXISTING_PER_POOL_KEYS = {
    "cpu_used_m",
    "cpu_alloc_m",
    "mem_used_mi",
    "mem_alloc_mi",
    "gpu_used",
    "gpu_alloc",
    "workload_cpu_m",
    "workload_mem_mi",
    "workload_gpu",
    "ds_cpu_m",
    "ds_mem_mi",
    "ds_gpu",
    "alloc_cpu_m_raw",
    "alloc_mem_mi_raw",
    "alloc_gpu_raw",
}
NODE_COUNTS_KEY = "node_counts_by_type"


def _clean_flags() -> dict:
    """Flags that suppress warmup + placeholders + phantom load.

    Node creation is then driven only by arrival placement (step 4c), so the
    live node set per pool is fully determined by the job shapes under test.
    """
    return {
        "seed": 42,
        "progress": False,
        "placeholders_enabled": False,
        "warmup_buckets_default": 0,
        "warmup_buckets_gpu": 0,
        "warmup_buckets_baremetal": 0,
    }


def _single_type_model() -> ClusterModel:
    fleets = {"cpu": FleetSpec(name="cpu", is_gpu=False, instances=("c7i.8xlarge",))}
    return ClusterModel(fleets_override=fleets)


def _mixed_type_model() -> ClusterModel:
    fleets = {"mixed": FleetSpec(name="mixed", is_gpu=False, instances=("c7i.8xlarge", "c7i.48xlarge"))}
    return ClusterModel(fleets_override=fleets)


def _one_node_per_job(pool: str, count: int, cpu_m: int) -> list[Job]:
    return [
        Job(label=f"j{i}", pool=pool, cpu_m=cpu_m, mem_mi=4096, gpu=0, start_bucket=0, end_bucket=600)
        for i in range(count)
    ]


def test_node_counts_present_for_every_present_pool():
    model = _single_type_model()
    sim = simulate(_one_node_per_job("cpu", 3, 20000), model=model, **_clean_flags())
    assert sim["per_bucket"]
    for _t, per_pool in sim["per_bucket"]:
        for _name, sums in per_pool.items():
            assert NODE_COUNTS_KEY in sums
            counts = sums[NODE_COUNTS_KEY]
            assert isinstance(counts, dict)
            assert counts
            assert all(isinstance(v, int) and v > 0 for v in counts.values())


def test_node_counts_sum_matches_measured_allocatable_single_type():
    model = _single_type_model()
    per_node_cpu = model.allocatable("cpu", "c7i.8xlarge").cpu_m
    sim = simulate(_one_node_per_job("cpu", 3, 20000), model=model, **_clean_flags())
    for _t, per_pool in sim["per_bucket"]:
        sums = per_pool["cpu"]
        counts = sums[NODE_COUNTS_KEY]
        assert set(counts) == {"c7i.8xlarge"}
        total_nodes = sum(counts.values())
        assert total_nodes == 3
        assert total_nodes * per_node_cpu == sums["alloc_cpu_m_raw"]


def test_node_counts_breaks_down_mixed_instance_types():
    model = _mixed_type_model()
    alloc_small = model.allocatable("mixed", "c7i.8xlarge")
    alloc_large = model.allocatable("mixed", "c7i.48xlarge")
    jobs = [
        Job(label="small", pool="mixed", cpu_m=2000, mem_mi=4096, gpu=0, start_bucket=0, end_bucket=600),
        Job(label="large", pool="mixed", cpu_m=190000, mem_mi=200000, gpu=0, start_bucket=0, end_bucket=600),
    ]
    sim = simulate(jobs, model=model, **_clean_flags())
    for _t, per_pool in sim["per_bucket"]:
        sums = per_pool["mixed"]
        counts = sums[NODE_COUNTS_KEY]
        assert counts == {"c7i.8xlarge": 1, "c7i.48xlarge": 1}
        expected_alloc_cpu = counts["c7i.8xlarge"] * alloc_small.cpu_m + counts["c7i.48xlarge"] * alloc_large.cpu_m
        expected_alloc_mem = counts["c7i.8xlarge"] * alloc_small.mem_mi + counts["c7i.48xlarge"] * alloc_large.mem_mi
        assert expected_alloc_cpu == sums["alloc_cpu_m_raw"]
        assert expected_alloc_mem == sums["alloc_mem_mi_raw"]


def test_mixed_breakdown_is_order_independent():
    model = _mixed_type_model()
    jobs = [
        Job(label="small", pool="mixed", cpu_m=2000, mem_mi=4096, gpu=0, start_bucket=0, end_bucket=600),
        Job(label="large", pool="mixed", cpu_m=190000, mem_mi=200000, gpu=0, start_bucket=0, end_bucket=600),
    ]
    for seed in (0, 1, 7, 42, 99):
        flags = _clean_flags()
        flags["seed"] = seed
        sim = simulate(jobs, model=model, **flags)
        last = sim["per_bucket"][-1][1]["mixed"][NODE_COUNTS_KEY]
        assert last == {"c7i.8xlarge": 1, "c7i.48xlarge": 1}


def test_change_is_additive_exact_key_set():
    model = _single_type_model()
    sim = simulate(_one_node_per_job("cpu", 2, 20000), model=model, **_clean_flags())
    for _t, per_pool in sim["per_bucket"]:
        for _name, sums in per_pool.items():
            assert set(sums) == EXISTING_PER_POOL_KEYS | {NODE_COUNTS_KEY}


def test_per_bucket_entry_remains_two_tuple():
    model = _single_type_model()
    sim = simulate(_one_node_per_job("cpu", 1, 20000), model=model, **_clean_flags())
    for entry in sim["per_bucket"]:
        assert len(entry) == 2
        t, per_pool = entry
        assert isinstance(t, int)
        assert isinstance(per_pool, dict)


def _gpu_model() -> ClusterModel:
    fleets = {"g5": FleetSpec(name="g5", is_gpu=True, instances=("g5.8xlarge",))}
    return ClusterModel(fleets_override=fleets)


def _make_node(**overrides) -> Node:
    base = {
        "pool": "cpu",
        "instance_type": "c7i.8xlarge",
        "cpu_allocatable_m": 30000,
        "mem_allocatable_mi": 60000,
        "gpu_allocatable": 0,
    }
    base.update(overrides)
    return Node(**base)


def test_pool_class_classifies_prefixes():
    assert _pool_class("p4d") == "baremetal"
    assert _pool_class("p6-b200") == "baremetal"
    assert _pool_class("g5") == "gpu"
    assert _pool_class("g4dn-metal") == "gpu"
    assert _pool_class("c7i-runner") == "default"
    assert _pool_class("r7a") == "default"


def test_warmup_buckets_per_class():
    assert _warmup_buckets("p5", 1, 2, 3) == 3
    assert _warmup_buckets("g6", 1, 2, 3) == 2
    assert _warmup_buckets("c7i", 1, 2, 3) == 1


def test_mark_empty_if_needed_skips_warming_node():
    node = _make_node(warming_until_bucket=4, cpu_used_m=100, mem_used_mi=100)
    _mark_empty_if_needed(node, bucket_idx=5)
    assert node.empty_since_bucket is None
    assert node.cpu_used_m == 100


def test_mark_empty_if_needed_zeroes_and_stamps_empty_node():
    node = _make_node(cpu_used_m=1, mem_used_mi=1, gpu_used=0, live_jobs=0)
    _mark_empty_if_needed(node, bucket_idx=7)
    assert node.cpu_used_m == 0
    assert node.mem_used_mi == 0
    assert node.gpu_used == 0
    assert node.empty_since_bucket == 7


def test_mark_empty_if_needed_preserves_existing_empty_stamp():
    node = _make_node(live_jobs=0, empty_since_bucket=2)
    _mark_empty_if_needed(node, bucket_idx=9)
    assert node.empty_since_bucket == 2


def test_mark_empty_if_needed_ignores_occupied_node():
    node = _make_node(live_jobs=1, cpu_used_m=500)
    _mark_empty_if_needed(node, bucket_idx=3)
    assert node.empty_since_bucket is None
    assert node.cpu_used_m == 500


def test_place_free_skips_warming_unless_included():
    warm = _make_node()
    warming = _make_node(warming_until_bucket=5)
    assert _place_free([warming], 1000, 1000, 0) is None
    assert _place_free([warming], 1000, 1000, 0, include_warming=True) is warming
    assert _place_free([warming, warm], 1000, 1000, 0) is warm


def test_place_free_picks_most_allocated_candidate():
    empty = _make_node()
    loaded = _make_node(cpu_used_m=20000)
    picked = _place_free([empty, loaded], 1000, 1000, 0)
    assert picked is loaded


def test_place_free_returns_none_when_nothing_fits():
    full = _make_node(cpu_used_m=30000)
    assert _place_free([full], 1000, 1000, 0) is None


def test_preempt_placeholder_matches_shape_on_best_node():
    ph = Placeholder(cpu_m=1000, mem_mi=2000, gpu=0, created_bucket=0)
    node = _make_node(cpu_used_m=1000, mem_used_mi=2000, placeholders=[ph])
    result = _preempt_placeholder([node], 1000, 2000, 0)
    assert result == (node, ph)


def test_preempt_placeholder_ignores_shape_mismatch_and_warming():
    other = Placeholder(cpu_m=500, mem_mi=500, gpu=0, created_bucket=0)
    node = _make_node(placeholders=[other])
    warming = _make_node(warming_until_bucket=3, placeholders=[Placeholder(1000, 2000, 0, 0)])
    assert _preempt_placeholder([node], 1000, 2000, 0) is None
    assert _preempt_placeholder([warming], 1000, 2000, 0) is None
    assert _preempt_placeholder([warming], 1000, 2000, 0, include_warming=True)[0] is warming


def test_place_phantom_skips_warming_nodes():
    warming = _make_node(warming_until_bucket=3)
    job = Job(label="p", pool="cpu", cpu_m=1000, mem_mi=1000, gpu=0, start_bucket=1, end_bucket=2)
    _place_phantom([warming], job, cap=0.5)
    assert warming.phantom_cpu_m == 0


def test_place_phantom_respects_cap_fraction():
    node = _make_node()
    job = Job(label="p", pool="cpu", cpu_m=20000, mem_mi=1000, gpu=0, start_bucket=1, end_bucket=2)
    _place_phantom([node], job, cap=0.10)
    assert node.phantom_cpu_m == 0


def test_place_phantom_skips_when_real_usage_leaves_no_room():
    node = _make_node(cpu_used_m=29900)
    job = Job(label="p", pool="cpu", cpu_m=1000, mem_mi=1000, gpu=0, start_bucket=1, end_bucket=2)
    _place_phantom([node], job, cap=0.99)
    assert node.phantom_cpu_m == 0


def test_place_phantom_places_when_room_and_under_cap():
    node = _make_node()
    job = Job(label="p", pool="cpu", cpu_m=2000, mem_mi=3000, gpu=0, start_bucket=1, end_bucket=2)
    _place_phantom([node], job, cap=0.90)
    assert node.phantom_cpu_m == 2000
    assert node.phantom_mem_mi == 3000


def test_place_phantom_skips_when_memory_does_not_fit():
    node = _make_node(mem_used_mi=59900)
    job = Job(label="p", pool="cpu", cpu_m=1000, mem_mi=1000, gpu=0, start_bucket=1, end_bucket=2)
    _place_phantom([node], job, cap=0.99)
    assert node.phantom_cpu_m == 0
    assert node.phantom_mem_mi == 0


def test_place_phantom_skips_when_gpu_does_not_fit():
    node = _make_node(gpu_allocatable=1, gpu_used=1)
    job = Job(label="p", pool="cpu", cpu_m=1000, mem_mi=1000, gpu=1, start_bucket=1, end_bucket=2)
    _place_phantom([node], job, cap=0.99)
    assert node.phantom_gpu == 0


def test_place_phantom_skips_when_memory_cap_exceeded():
    node = _make_node()
    job = Job(label="p", pool="cpu", cpu_m=1000, mem_mi=30000, gpu=0, start_bucket=1, end_bucket=2)
    _place_phantom([node], job, cap=0.10)
    assert node.phantom_cpu_m == 0
    assert node.phantom_mem_mi == 0


def test_place_phantom_skips_when_gpu_cap_exceeded():
    node = _make_node(gpu_allocatable=8)
    job = Job(label="p", pool="cpu", cpu_m=100, mem_mi=100, gpu=4, start_bucket=1, end_bucket=2)
    _place_phantom([node], job, cap=0.40)
    assert node.phantom_gpu == 0


def test_warming_lookahead_provisions_and_promotes():
    model = _single_type_model()
    alloc = model.allocatable("cpu", "c7i.8xlarge")
    big = alloc.cpu_m - 500
    jobs = [
        Job(label="anchor", pool="cpu", cpu_m=big, mem_mi=1024, gpu=0, start_bucket=0, end_bucket=3000),
        Job(label="future", pool="cpu", cpu_m=big, mem_mi=1024, gpu=0, start_bucket=600, end_bucket=1800),
    ]
    sim = simulate(
        jobs,
        model=model,
        seed=1,
        progress=False,
        placeholders_enabled=False,
        warmup_buckets_default=2,
        warmup_buckets_gpu=2,
        warmup_buckets_baremetal=2,
    )
    assert sim["pool_max_warming"]["cpu"] >= 1
    assert sim["pool_total_created"]["cpu"] == 2


def test_lookahead_skips_when_free_capacity_available():
    model = _single_type_model()
    jobs = [
        Job(label="anchor", pool="cpu", cpu_m=1000, mem_mi=1024, gpu=0, start_bucket=0, end_bucket=3000),
        Job(label="future", pool="cpu", cpu_m=2000, mem_mi=4096, gpu=0, start_bucket=600, end_bucket=1800),
    ]
    sim = simulate(
        jobs,
        model=model,
        seed=1,
        progress=False,
        placeholders_enabled=False,
        warmup_buckets_default=1,
        warmup_buckets_gpu=1,
        warmup_buckets_baremetal=3,
    )
    assert sim["pool_total_created"]["cpu"] == 1


def test_gpu_lookahead_uses_gpu_warmup_window():
    model = _gpu_model()
    alloc = model.allocatable("g5", "g5.8xlarge")
    jobs = [
        Job(label="anchor", pool="g5", cpu_m=alloc.cpu_m - 200, mem_mi=1024, gpu=1, start_bucket=0, end_bucket=3000),
        Job(label="future", pool="g5", cpu_m=alloc.cpu_m - 200, mem_mi=1024, gpu=1, start_bucket=600, end_bucket=1800),
    ]
    sim = simulate(
        jobs,
        model=model,
        seed=1,
        progress=False,
        placeholders_enabled=False,
        warmup_buckets_default=1,
        warmup_buckets_gpu=2,
        warmup_buckets_baremetal=3,
    )
    for _t, per_pool in sim["per_bucket"]:
        if "g5" in per_pool:
            assert per_pool["g5"]["gpu_alloc"] >= 1
    assert sim["pool_total_created"]["g5"] >= 2


def test_placeholders_created_and_expire():
    model = _single_type_model()
    jobs = [Job(label="a", pool="cpu", cpu_m=1000, mem_mi=1024, gpu=0, start_bucket=0, end_bucket=1800)]
    sim = simulate(
        jobs,
        model=model,
        seed=1,
        progress=False,
        placeholders_enabled=True,
        placeholder_max_age=2,
        warmup_buckets_default=0,
        warmup_buckets_gpu=0,
        warmup_buckets_baremetal=0,
    )
    assert sim["pool_total_placeholders"]["cpu"] >= 1
    assert sim["pool_total_expired"]["cpu"] >= 1


def test_placeholder_preemption_reuses_matching_shape():
    model = _single_type_model()
    alloc = model.allocatable("cpu", "c7i.8xlarge")
    big = alloc.cpu_m - 5000
    jobs = [Job(label="a", pool="cpu", cpu_m=big, mem_mi=1024, gpu=0, start_bucket=0, end_bucket=1800)]
    sim = simulate(
        jobs,
        model=model,
        seed=1,
        progress=False,
        placeholders_enabled=True,
        placeholder_max_age=5,
        warmup_buckets_default=0,
        warmup_buckets_gpu=0,
        warmup_buckets_baremetal=0,
    )
    assert sim["pool_total_placeholders"]["cpu"] >= 1
    assert sim["pool_total_preempted"]["cpu"] >= 1


def test_same_bucket_start_equals_end_finisher():
    model = _single_type_model()
    jobs = [
        Job(label="instant", pool="cpu", cpu_m=30000, mem_mi=50000, gpu=0, start_bucket=0, end_bucket=0),
        Job(label="anchor", pool="cpu", cpu_m=1000, mem_mi=1024, gpu=0, start_bucket=0, end_bucket=600),
    ]
    sim = simulate(jobs, model=model, **_clean_flags())
    assert sim["pool_total_created"]["cpu"] >= 1
    first = sim["per_bucket"][0][1]["cpu"]
    assert first["workload_cpu_m"] == 1000


def test_consolidation_drops_empty_node():
    model = _single_type_model()
    jobs = [
        Job(label="short", pool="cpu", cpu_m=30000, mem_mi=50000, gpu=0, start_bucket=0, end_bucket=300),
        Job(label="long", pool="cpu", cpu_m=30000, mem_mi=50000, gpu=0, start_bucket=0, end_bucket=3000),
    ]
    sim = simulate(jobs, model=model, **_clean_flags(), empty_ttl_buckets=2)
    assert sim["pool_max_nodes"]["cpu"] == 2
    final = sim["per_bucket"][-1][1]["cpu"]
    assert sum(final["node_counts_by_type"].values()) == 1


def test_consolidation_drops_entire_pool():
    fleets = {
        "a": FleetSpec(name="a", is_gpu=False, instances=("c7i.8xlarge",)),
        "b": FleetSpec(name="b", is_gpu=False, instances=("c7i.8xlarge",)),
    }
    model = ClusterModel(fleets_override=fleets)
    jobs = [
        Job(label="a0", pool="a", cpu_m=30000, mem_mi=50000, gpu=0, start_bucket=0, end_bucket=300),
        Job(label="b0", pool="b", cpu_m=30000, mem_mi=50000, gpu=0, start_bucket=0, end_bucket=3000),
    ]
    sim = simulate(jobs, model=model, **_clean_flags(), empty_ttl_buckets=2)
    last = sim["per_bucket"][-1][1]
    assert "a" not in last
    assert "b" in last


def test_phantom_load_inflates_measured_usage():
    model = _single_type_model()
    jobs = [
        Job(label="now", pool="cpu", cpu_m=2000, mem_mi=4096, gpu=0, start_bucket=0, end_bucket=1200),
        Job(label="soon", pool="cpu", cpu_m=2000, mem_mi=4096, gpu=0, start_bucket=300, end_bucket=1200),
    ]
    sim = simulate(
        jobs,
        model=model,
        seed=1,
        progress=False,
        placeholders_enabled=False,
        warmup_buckets_default=0,
        warmup_buckets_gpu=0,
        warmup_buckets_baremetal=0,
        phantom_pods_enabled=True,
        phantom_lookahead_buckets=1,
        phantom_cap=0.30,
    )
    first = sim["per_bucket"][0][1]["cpu"]
    assert first["cpu_used_m"] > first["workload_cpu_m"]


def test_phantom_creates_empty_pool_entry_skipped_at_measure():
    fleets = {
        "a": FleetSpec(name="a", is_gpu=False, instances=("c7i.8xlarge",)),
        "b": FleetSpec(name="b", is_gpu=False, instances=("c7i.8xlarge",)),
    }
    model = ClusterModel(fleets_override=fleets)
    jobs = [
        Job(label="a-now", pool="a", cpu_m=2000, mem_mi=4096, gpu=0, start_bucket=0, end_bucket=1200),
        Job(label="b-future", pool="b", cpu_m=2000, mem_mi=4096, gpu=0, start_bucket=300, end_bucket=1200),
    ]
    sim = simulate(
        jobs,
        model=model,
        seed=1,
        progress=False,
        placeholders_enabled=False,
        warmup_buckets_default=0,
        warmup_buckets_gpu=0,
        warmup_buckets_baremetal=0,
        phantom_pods_enabled=True,
        phantom_lookahead_buckets=1,
        phantom_cap=0.30,
    )
    assert "b" not in sim["per_bucket"][0][1]


def test_daemonsets_in_metric_adds_overhead_to_alloc():
    model = _single_type_model()
    jobs = _one_node_per_job("cpu", 1, 20000)
    with_ds = simulate(
        jobs,
        model=model,
        seed=1,
        progress=False,
        placeholders_enabled=False,
        warmup_buckets_default=0,
        warmup_buckets_gpu=0,
        warmup_buckets_baremetal=0,
        daemonsets_in_metric=True,
    )
    without_ds = simulate(
        jobs,
        model=model,
        seed=1,
        progress=False,
        placeholders_enabled=False,
        warmup_buckets_default=0,
        warmup_buckets_gpu=0,
        warmup_buckets_baremetal=0,
        daemonsets_in_metric=False,
    )
    a = with_ds["per_bucket"][0][1]["cpu"]
    b = without_ds["per_bucket"][0][1]["cpu"]
    assert a["cpu_alloc_m"] > b["cpu_alloc_m"]


def test_progress_prints_to_stderr(capsys):
    model = _single_type_model()
    jobs = [Job(label="a", pool="cpu", cpu_m=1000, mem_mi=1024, gpu=0, start_bucket=0, end_bucket=300)]
    simulate(
        jobs,
        model=model,
        seed=1,
        progress=True,
        placeholders_enabled=False,
        warmup_buckets_default=0,
        warmup_buckets_gpu=0,
        warmup_buckets_baremetal=0,
    )
    err = capsys.readouterr().err
    assert "bucket" in err


def _run_main(monkeypatch, argv: list[str]) -> int:
    monkeypatch.setattr(sys, "argv", ["simulate.py", *argv])
    return main()


def test_main_runs_end_to_end_on_sample_csv(monkeypatch, capsys):
    rc = _run_main(monkeypatch, [str(SAMPLE_CSV), "--no-progress"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Simulation flags" in out
    assert "Per-pool utilization" in out


def test_main_honors_all_toggle_flags(monkeypatch, capsys):
    rc = _run_main(
        monkeypatch,
        [
            str(SAMPLE_CSV),
            "--no-progress",
            "--no-warmup",
            "--no-placeholders",
            "--no-runner-pods",
            "--daemonsets-in-metric",
            "--phantom-pods",
            "--phantom-lookahead-buckets",
            "2",
            "--phantom-cap",
            "0.5",
            "--empty-ttl-buckets",
            "3",
            "--placeholder-max-age",
            "3",
            "--seed",
            "7",
        ],
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "phantom_pods: on" in out
    assert "placeholders: off" in out


def test_main_drop_provider_and_keep_fraction(monkeypatch, capsys):
    rc = _run_main(
        monkeypatch,
        [str(SAMPLE_CSV), "--no-progress", "--drop-provider", "lf", "--keep-fraction", "0.5"],
    )
    assert rc == 0


def test_main_last_days_filter(monkeypatch, capsys):
    rc = _run_main(monkeypatch, [str(SAMPLE_CSV), "--no-progress", "--last-days", "365"])
    assert rc == 0


def test_main_returns_one_when_no_jobs(monkeypatch, tmp_path, capsys):
    empty_csv = tmp_path / "empty.csv"
    empty_csv.write_text("provider,label,nodepool,nodepool_fraction,start_time,end_time\n")
    rc = _run_main(monkeypatch, [str(empty_csv), "--no-progress"])
    assert rc == 1
    assert "no jobs" in capsys.readouterr().err
