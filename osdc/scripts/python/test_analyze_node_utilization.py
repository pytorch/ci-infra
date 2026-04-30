"""Tests for analyze_node_utilization + utilization_report + packing modules.

Synthetic ClusterTopology fixtures follow the pattern from test_cluster_topology.py.
"""

from __future__ import annotations

from unittest.mock import patch

from analyze_node_utilization import (
    HOOKS_OVERHEAD_CPU_M,
    HOOKS_OVERHEAD_MEM_MI,
    compute_allocatable,
    compute_daemonset_overhead,
    compute_node_slack,
    find_maximal_combos,
    find_valid_combos,
    format_mem,
    kubelet_reserved,
    main,
    parse_args,
    parse_memory,
    per_runner_pod_total,
    per_workflow_pod_total,
    print_combo,
    print_runner_pool_section,
    print_unschedulable_section,
    print_workflow_pool_section,
)
from cluster_topology import ClusterTopology, NodePoolEntry, RunnerEntry
from daemonset_overhead import DaemonSetOverhead
from utilization_report import analyze_pool

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _make_runner(**overrides) -> RunnerEntry:
    base = {
        "name": "r1",
        "scale_set_name": "c-mt-r1",
        "instance_type": "c7a.48xlarge",
        "workflow_fleet": "c7a",
        "runner_class": None,
        "runner_pod_cpu_m": 750,
        "runner_pod_mem_mi": 512,
        "workflow_pod_cpu_m": 8000,
        "workflow_pod_mem_mi": 16384,
        "workflow_pod_gpu": 0,
        "schedulable": True,
        "schedulable_reason": None,
    }
    base.update(overrides)
    return RunnerEntry(**base)


def _make_pool(**overrides) -> NodePoolEntry:
    base = {
        "name": "c7a-48xlarge",
        "fleet": "c7a",
        "instance_type": "c7a.48xlarge",
        "arch": "amd64",
        "gpu": False,
        "runner_class": None,
    }
    base.update(overrides)
    return NodePoolEntry(**base)


def _make_topology(**overrides) -> ClusterTopology:
    base = {
        "cluster_id": "test-cluster",
        "region": "us-east-2",
        "modules": ["nodepools", "arc-runners"],
        "nodepools": [],
        "runner_pool_fleet": "c7i-runner",
        "workflow_pool_fleets": {"c7a"},
        "runners": [],
    }
    base.update(overrides)
    return ClusterTopology(**base)


FAKE_DS = [
    DaemonSetOverhead("kube-proxy", 50, 80, False, "test", fleet_selector=None),
    DaemonSetOverhead("gpu-plugin", 100, 256, True, "test", fleet_selector=None),
]


# ---------------------------------------------------------------------------
# kubelet_reserved
# ---------------------------------------------------------------------------


class TestKubeletReserved:
    def test_single_core(self):
        cpu, mem = kubelet_reserved(1, 4, 10)
        assert cpu == 60
        assert mem == 255 + 11 * 10 + 100

    def test_two_cores(self):
        cpu, _ = kubelet_reserved(2, 8, 20)
        assert cpu == 70

    def test_four_cores(self):
        cpu, _ = kubelet_reserved(4, 16, 50)
        assert cpu == 80

    def test_many_cores(self):
        cpu, _ = kubelet_reserved(192, 384, 737)
        assert cpu == 80 + int((192 - 4) * 2.5)

    def test_memory_scales_with_max_pods(self):
        _, mem_low = kubelet_reserved(4, 16, 10)
        _, mem_high = kubelet_reserved(4, 16, 100)
        assert mem_high > mem_low


# ---------------------------------------------------------------------------
# compute_daemonset_overhead (signature now takes is_gpu kw + optional fleet_name)
# ---------------------------------------------------------------------------


class TestComputeDaemonsetOverhead:
    def test_cpu_only_node(self):
        cpu, mem = compute_daemonset_overhead(FAKE_DS, is_gpu=False)
        assert cpu == 50
        assert mem == 80

    def test_gpu_node(self):
        cpu, mem = compute_daemonset_overhead(FAKE_DS, is_gpu=True)
        assert cpu == 150
        assert mem == 336

    def test_empty_daemonsets(self):
        cpu, mem = compute_daemonset_overhead([], is_gpu=True)
        assert cpu == 0
        assert mem == 0

    def test_fleet_filter_excludes_pinned_to_other(self):
        ds = [
            DaemonSetOverhead("a", 10, 32, False, "test", fleet_selector=None),
            DaemonSetOverhead("b", 20, 64, False, "test", fleet_selector="c7i-runner"),
            DaemonSetOverhead("c", 30, 128, False, "test", fleet_selector="m8g"),
        ]
        cpu, mem = compute_daemonset_overhead(ds, is_gpu=False, fleet_name="c7i-runner")
        # a (None) + b (c7i-runner) only.
        assert cpu == 30
        assert mem == 96

    def test_fleet_none_keeps_all(self):
        ds = [
            DaemonSetOverhead("b", 20, 64, False, "test", fleet_selector="c7i-runner"),
            DaemonSetOverhead("c", 30, 128, False, "test", fleet_selector="m8g"),
        ]
        cpu, mem = compute_daemonset_overhead(ds, is_gpu=False, fleet_name=None)
        # Legacy: no fleet_name => include both.
        assert cpu == 50
        assert mem == 192


# ---------------------------------------------------------------------------
# parse_memory
# ---------------------------------------------------------------------------


class TestParseMemory:
    def test_gibibytes(self):
        assert parse_memory("4Gi") == 4096

    def test_mebibytes(self):
        assert parse_memory("256Mi") == 256

    def test_kibibytes(self):
        assert parse_memory("1024Ki") == 1

    def test_plain_bytes(self):
        assert parse_memory("134217728") == 128


# ---------------------------------------------------------------------------
# format_mem
# ---------------------------------------------------------------------------


class TestFormatMem:
    def test_mib_small(self):
        assert format_mem(512) == "512Mi"

    def test_gib_exact(self):
        assert format_mem(1024) == "1.0Gi"

    def test_gib_fractional(self):
        assert format_mem(1536) == "1.5Gi"

    def test_negative_large(self):
        assert format_mem(-2048) == "-2.0Gi"

    def test_negative_small(self):
        assert format_mem(-512) == "-512Mi"


# ---------------------------------------------------------------------------
# per_runner_pod_total / per_workflow_pod_total (RunnerEntry-based)
# ---------------------------------------------------------------------------


class TestPerRunnerPodTotal:
    def test_basic(self):
        r = _make_runner(runner_pod_cpu_m=750, runner_pod_mem_mi=512)
        assert per_runner_pod_total(r) == (750, 512, 0)

    def test_drops_workflow_fields(self):
        r = _make_runner(workflow_pod_cpu_m=8000, workflow_pod_mem_mi=16384, workflow_pod_gpu=2)
        # Runner pod doesn't get workflow CPU/mem/GPU.
        assert per_runner_pod_total(r) == (r.runner_pod_cpu_m, r.runner_pod_mem_mi, 0)


class TestPerWorkflowPodTotal:
    def test_no_gpu_adds_hooks(self):
        r = _make_runner(workflow_pod_cpu_m=8000, workflow_pod_mem_mi=16384, workflow_pod_gpu=0)
        cpu, mem, gpu = per_workflow_pod_total(r)
        assert cpu == 8000 + HOOKS_OVERHEAD_CPU_M
        assert mem == 16384 + HOOKS_OVERHEAD_MEM_MI
        assert gpu == 0

    def test_with_gpu(self):
        r = _make_runner(workflow_pod_cpu_m=16000, workflow_pod_mem_mi=32768, workflow_pod_gpu=4)
        cpu, mem, gpu = per_workflow_pod_total(r)
        assert cpu == 16000 + HOOKS_OVERHEAD_CPU_M
        assert mem == 32768 + HOOKS_OVERHEAD_MEM_MI
        assert gpu == 4


# ---------------------------------------------------------------------------
# compute_allocatable (signature gained fleet_name)
# ---------------------------------------------------------------------------


class TestComputeAllocatable:
    def test_known_instance_type(self):
        ds = [DaemonSetOverhead("ds1", 100, 200, False, "test")]
        alloc = compute_allocatable("c7a.48xlarge", ds)
        assert alloc is not None
        assert alloc["total_cpu_m"] == 192 * 1000
        assert alloc["allocatable_cpu_m"] < alloc["total_cpu_m"]
        assert alloc["allocatable_mem_mi"] < alloc["total_mem_mi"]
        assert alloc["is_gpu"] is False

    def test_unknown_instance_type(self):
        assert compute_allocatable("z99.nonexistent", []) is None

    def test_gpu_instance(self):
        ds = [DaemonSetOverhead("gpu-ds", 100, 200, True, "test")]
        alloc = compute_allocatable("g5.8xlarge", ds)
        assert alloc is not None
        assert alloc["is_gpu"] is True
        assert alloc["allocatable_gpu"] == 1

    def test_fleet_name_filters_ds(self):
        ds = [
            DaemonSetOverhead("base", 100, 256, False, "test", fleet_selector=None),
            DaemonSetOverhead("c7i-only", 50, 128, False, "test", fleet_selector="c7i-runner"),
            DaemonSetOverhead("m8g-only", 25, 64, False, "test", fleet_selector="m8g"),
        ]
        alloc_runner = compute_allocatable("c7a.48xlarge", ds, fleet_name="c7i-runner")
        alloc_m8g = compute_allocatable("c7a.48xlarge", ds, fleet_name="m8g")
        assert alloc_runner is not None
        assert alloc_m8g is not None
        # Different fleet_name => different DS overhead.
        assert alloc_runner["ds_cpu_m"] != alloc_m8g["ds_cpu_m"]
        assert alloc_runner["ds_cpu_m"] == 100 + 50  # base + c7i-only
        assert alloc_m8g["ds_cpu_m"] == 100 + 25  # base + m8g-only

    def test_legacy_no_fleet_includes_all_ds(self):
        ds = [
            DaemonSetOverhead("base", 100, 256, False, "test", fleet_selector=None),
            DaemonSetOverhead("c7i-only", 50, 128, False, "test", fleet_selector="c7i-runner"),
        ]
        alloc = compute_allocatable("c7a.48xlarge", ds)
        # Legacy path: include everything.
        assert alloc["ds_cpu_m"] == 150


# ---------------------------------------------------------------------------
# packing helpers (now take pod_cost tuples + names)
# ---------------------------------------------------------------------------


class TestFindValidCombos:
    def test_single_runner_fits(self):
        alloc = {"allocatable_cpu_m": 10000, "allocatable_mem_mi": 20000, "allocatable_gpu": 0}
        combos = find_valid_combos([(2000, 4096, 0)], ["r1"], alloc, max_pods=5)
        assert len(combos) > 0
        assert all(c["cpu_used_m"] <= 10000 for c in combos)

    def test_no_fit(self):
        alloc = {"allocatable_cpu_m": 100, "allocatable_mem_mi": 100, "allocatable_gpu": 0}
        combos = find_valid_combos([(8000, 16384, 0)], ["r1"], alloc, max_pods=5)
        assert combos == []


class TestFindMaximalCombos:
    def test_filters_non_maximal(self):
        alloc = {"allocatable_cpu_m": 10000, "allocatable_mem_mi": 20000, "allocatable_gpu": 0}
        pod_costs = [(2000, 4096, 0)]
        combos = find_valid_combos(pod_costs, ["r1"], alloc, max_pods=5)
        maximal = find_maximal_combos(combos, alloc, pod_costs)
        for combo in maximal:
            c, m, _g = pod_costs[0]
            remaining_cpu = alloc["allocatable_cpu_m"] - combo["cpu_used_m"]
            remaining_mem = alloc["allocatable_mem_mi"] - combo["mem_used_mi"]
            assert remaining_cpu < c or remaining_mem < m


class TestComputeNodeSlack:
    def test_homogeneous_only(self):
        alloc = {"allocatable_cpu_m": 10000, "allocatable_mem_mi": 20000, "allocatable_gpu": 0}
        slack = compute_node_slack(alloc, [(2000, 4096, 0)], homogeneous_only=True)
        assert slack is not None
        assert "min_cpu_m" in slack

    def test_many_runners_forces_homogeneous(self):
        """More than 8 pod costs forces homogeneous-only path."""
        alloc = {"allocatable_cpu_m": 100000, "allocatable_mem_mi": 200000, "allocatable_gpu": 0}
        slack = compute_node_slack(alloc, [(2000, 4096, 0)] * 10)
        assert slack is not None

    def test_no_valid_combos(self):
        alloc = {"allocatable_cpu_m": 100, "allocatable_mem_mi": 100, "allocatable_gpu": 0}
        slack = compute_node_slack(alloc, [(8000, 16384, 0)], homogeneous_only=True)
        assert slack is None

    def test_mixed_combos_path(self):
        alloc = {"allocatable_cpu_m": 20000, "allocatable_mem_mi": 40000, "allocatable_gpu": 0}
        slack = compute_node_slack(alloc, [(2000, 4096, 0), (4000, 8192, 0)], homogeneous_only=False)
        assert slack is not None

    def test_mixed_no_maximal_returns_none(self):
        alloc = {"allocatable_cpu_m": 100, "allocatable_mem_mi": 100, "allocatable_gpu": 0}
        slack = compute_node_slack(alloc, [(8000, 16384, 0)], homogeneous_only=False)
        assert slack is None


# ---------------------------------------------------------------------------
# print_combo (smoke output)
# ---------------------------------------------------------------------------


class TestPrintCombo:
    def test_prints_combo_no_gpu(self, capsys):
        combo = {
            "runners": ["r1", "r1", "r2"],
            "cpu_util": 85.0,
            "mem_util": 70.0,
            "gpu_util": 0.0,
            "cpu_waste_m": 1500,
            "mem_waste_mi": 6000,
        }
        print_combo(combo, {"allocatable_gpu": 0}, 90.0, 1)
        assert "r1" in capsys.readouterr().out

    def test_prints_combo_with_gpu(self, capsys):
        combo = {
            "runners": ["r1"],
            "cpu_util": 95.0,
            "mem_util": 90.0,
            "gpu_util": 100.0,
            "cpu_waste_m": 500,
            "mem_waste_mi": 2000,
        }
        print_combo(combo, {"allocatable_gpu": 4}, 90.0, 1)
        assert "GPU" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# analyze_pool (per-pool analyzer used by both section printers)
# ---------------------------------------------------------------------------


class TestAnalyzePool:
    def test_runs_workflow_pool(self, capsys):
        runners = [
            _make_runner(name="r1", workflow_pod_cpu_m=8000, workflow_pod_mem_mi=16384),
            _make_runner(name="r2", workflow_pod_cpu_m=16000, workflow_pod_mem_mi=32768),
        ]
        nodepools = [_make_pool()]
        below, slacks = analyze_pool(
            "Test Pool",
            "c7a",
            nodepools,
            runners,
            FAKE_DS,
            threshold=90.0,
            pod_total_fn=per_workflow_pod_total,
            compute_allocatable_fn=compute_allocatable,
        )
        out = capsys.readouterr().out
        assert "Test Pool" in out
        assert "r1" in out
        assert "r2" in out
        assert isinstance(below, int)
        assert "c7a.48xlarge" in slacks

    def test_runs_runner_pool(self, capsys):
        runners = [_make_runner(name="r1"), _make_runner(name="r2")]
        nodepools = [_make_pool(name="c7i-runner-12xl", fleet="c7i-runner", instance_type="c7i.12xlarge")]
        below, slacks = analyze_pool(
            "Runner Pool",
            "c7i-runner",
            nodepools,
            runners,
            FAKE_DS,
            threshold=90.0,
            pod_total_fn=per_runner_pod_total,
            compute_allocatable_fn=compute_allocatable,
        )
        out = capsys.readouterr().out
        assert "Runner Pool" in out
        # Runner pods are tiny → many fit per node.
        assert "pods" in out
        assert isinstance(below, int)
        assert "c7i.12xlarge" in slacks

    def test_no_runners(self, capsys):
        below, slacks = analyze_pool(
            "Empty",
            "c7a",
            [_make_pool()],
            [],
            FAKE_DS,
            threshold=90.0,
            pod_total_fn=per_workflow_pod_total,
            compute_allocatable_fn=compute_allocatable,
        )
        assert below == 0
        assert slacks == {}
        assert "no runners" in capsys.readouterr().out

    def test_no_nodepools(self, capsys):
        below, slacks = analyze_pool(
            "NoNP",
            "c7a",
            [],
            [_make_runner()],
            FAKE_DS,
            threshold=90.0,
            pod_total_fn=per_workflow_pod_total,
            compute_allocatable_fn=compute_allocatable,
        )
        assert below == 0
        assert slacks == {}
        assert "no nodepools" in capsys.readouterr().out

    def test_unknown_instance_type_warning(self, capsys):
        nodepools = [_make_pool(name="z99-x", fleet="c7a", instance_type="z99.fake")]
        below, slacks = analyze_pool(
            "Unknown",
            "c7a",
            nodepools,
            [_make_runner()],
            FAKE_DS,
            threshold=90.0,
            pod_total_fn=per_workflow_pod_total,
            compute_allocatable_fn=compute_allocatable,
        )
        assert below == 0
        assert slacks == {}  # unknown skipped → no slack collected
        assert "z99.fake" in capsys.readouterr().out

    def test_too_many_runners_skips_mixed(self, capsys):
        # 10 runners triggers the > 8 path.
        runners = [_make_runner(name=f"r{i}") for i in range(10)]
        analyze_pool(
            "Big",
            "c7a",
            [_make_pool()],
            runners,
            FAKE_DS,
            threshold=90.0,
            pod_total_fn=per_workflow_pod_total,
            compute_allocatable_fn=compute_allocatable,
        )
        assert "too many runner types" in capsys.readouterr().out

    def test_no_valid_combos_message(self, capsys):
        # One huge runner that cannot fit on any node.
        huge = _make_runner(name="huge", workflow_pod_cpu_m=999000, workflow_pod_mem_mi=999000)
        analyze_pool(
            "Huge",
            "c7a",
            [_make_pool()],
            [huge],
            FAKE_DS,
            threshold=90.0,
            pod_total_fn=per_workflow_pod_total,
            compute_allocatable_fn=compute_allocatable,
        )
        assert "no valid combos found" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Section printers
# ---------------------------------------------------------------------------


class TestPrintWorkflowPoolSection:
    def test_runs_with_schedulable_runner(self, capsys):
        runners = [_make_runner(name="r1", workflow_fleet="c7a")]
        nodepools = [_make_pool()]
        topo = _make_topology(runners=runners, nodepools=nodepools, workflow_pool_fleets={"c7a"})
        below, slacks = print_workflow_pool_section(topo, FAKE_DS, threshold=90.0)
        out = capsys.readouterr().out
        assert "WORKFLOW POOL PACKING" in out
        assert "Workflow Pool [c7a]" in out
        assert isinstance(below, int)
        assert "c7a/c7a.48xlarge" in slacks

    def test_no_schedulable_runners(self, capsys):
        topo = _make_topology(runners=[_make_runner(schedulable=False, schedulable_reason="no pool")])
        below, slacks = print_workflow_pool_section(topo, FAKE_DS, threshold=90.0)
        assert below == 0
        assert slacks == {}
        assert "no schedulable runners" in capsys.readouterr().out

    def test_release_runners_separated(self, capsys):
        runners = [
            _make_runner(name="r-normal", workflow_fleet="c7a", runner_class=None),
            _make_runner(name="r-rel", workflow_fleet="c7a", runner_class="release"),
        ]
        nodepools = [
            _make_pool(),
            _make_pool(name="c7a-48xlarge-release", runner_class="release"),
        ]
        topo = _make_topology(runners=runners, nodepools=nodepools, workflow_pool_fleets={"c7a"})
        print_workflow_pool_section(topo, FAKE_DS, threshold=90.0)
        out = capsys.readouterr().out
        assert "Workflow Pool [c7a]" in out
        assert "Workflow Pool [c7a/release]" in out


class TestPrintRunnerPoolSection:
    def test_with_runner_pool(self, capsys):
        runners = [_make_runner(name="r1", workflow_fleet="c7a")]
        nodepools = [
            _make_pool(),
            _make_pool(name="c7i-runner-12xl", fleet="c7i-runner", instance_type="c7i.12xlarge"),
        ]
        topo = _make_topology(
            runners=runners, nodepools=nodepools, workflow_pool_fleets={"c7a"}, runner_pool_fleet="c7i-runner"
        )
        below, slacks = print_runner_pool_section(topo, FAKE_DS, threshold=90.0)
        out = capsys.readouterr().out
        assert "RUNNER POOL PACKING" in out
        assert "Runner Pool [c7i-runner]" in out
        assert isinstance(below, int)
        assert "c7i.12xlarge" in slacks

    def test_no_runner_pool_warns(self, capsys):
        topo = _make_topology(runner_pool_fleet=None)
        below, slacks = print_runner_pool_section(topo, FAKE_DS, threshold=90.0)
        assert below == 0
        assert slacks == {}
        out = capsys.readouterr().out
        assert "No c7i-runner pool found" in out


class TestPrintUnschedulableSection:
    def test_lists_unschedulable(self, capsys):
        runners = [
            _make_runner(name="ok"),
            _make_runner(name="bad", schedulable=False, schedulable_reason="missing fleet"),
        ]
        topo = _make_topology(runners=runners)
        print_unschedulable_section(topo)
        out = capsys.readouterr().out
        assert "UNSCHEDULABLE RUNNERS" in out
        assert "bad" in out
        assert "missing fleet" in out
        # Schedulable runners should NOT appear in this section.
        assert "ok " not in out

    def test_all_schedulable(self, capsys):
        topo = _make_topology(runners=[_make_runner(name="ok")])
        print_unschedulable_section(topo)
        out = capsys.readouterr().out
        assert "all runners are schedulable" in out


# ---------------------------------------------------------------------------
# parse_args / main
# ---------------------------------------------------------------------------


class TestParseArgs:
    def test_required_cluster(self):
        args = parse_args(["--cluster", "arc-staging"])
        assert args.cluster == "arc-staging"
        assert args.threshold == 90.0

    def test_threshold_override(self):
        args = parse_args(["--cluster", "x", "--threshold", "80"])
        assert args.threshold == 80.0


class TestMain:
    @patch("analyze_node_utilization.discover_daemonsets")
    @patch("analyze_node_utilization.resolve_cluster")
    def test_runs_with_synthetic_topology(self, mock_resolve, mock_discover, capsys):
        """main() runs without crashing when topology + daemonsets are stubbed."""
        runners = [
            _make_runner(name="r1", workflow_fleet="c7a"),
            _make_runner(name="bad", schedulable=False, schedulable_reason="fake"),
        ]
        nodepools = [
            _make_pool(),
            _make_pool(name="c7i-runner-12xl", fleet="c7i-runner", instance_type="c7i.12xlarge"),
        ]
        mock_resolve.return_value = _make_topology(
            cluster_id="fake-cluster",
            runners=runners,
            nodepools=nodepools,
            workflow_pool_fleets={"c7a"},
            runner_pool_fleet="c7i-runner",
        )
        mock_discover.return_value = FAKE_DS

        rc = main(["--cluster", "fake-cluster", "--threshold", "90"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Node Utilization Analysis" in out
        assert "WORKFLOW POOL PACKING" in out
        assert "RUNNER POOL PACKING" in out
        assert "UNSCHEDULABLE RUNNERS" in out

    @patch("analyze_node_utilization.discover_daemonsets")
    @patch("analyze_node_utilization.resolve_cluster")
    def test_main_no_runner_pool(self, mock_resolve, mock_discover, capsys):
        """main() handles topology without c7i-runner gracefully."""
        mock_resolve.return_value = _make_topology(runner_pool_fleet=None, workflow_pool_fleets={"c7a"})
        mock_discover.return_value = FAKE_DS
        rc = main(["--cluster", "x"])
        assert rc == 0
        assert "No c7i-runner pool found" in capsys.readouterr().out
