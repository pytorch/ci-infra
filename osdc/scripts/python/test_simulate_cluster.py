"""Tests for simulate_cluster module (split-pool model)."""

from __future__ import annotations

from cluster_topology import ClusterTopology, NodePoolEntry, RunnerEntry
from daemonset_overhead import DaemonSetOverhead
from simulate_cluster import (
    PoolUtilization,
    SimNode,
    SimResult,
    SimulationUtilization,
    best_fit_place_runner,
    best_fit_place_workflow,
    build_peak_targets,
    build_weighted_pool,
    compute_utilization,
    provision_runner_node,
    provision_workflow_node,
    run_simulation,
    weighted_mape,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

FAKE_DAEMONSETS = [
    DaemonSetOverhead("kube-proxy", 50, 80, False, "test"),
    DaemonSetOverhead("vpc-cni", 50, 128, False, "test"),
]


def _make_runner(
    name: str,
    *,
    instance_type: str = "c7a.48xlarge",
    workflow_fleet: str = "c7a",
    runner_class: str | None = None,
    runner_pod_cpu_m: int = 750,
    runner_pod_mem_mi: int = 512,
    workflow_pod_cpu_m: int = 8000,
    workflow_pod_mem_mi: int = 16 * 1024,
    workflow_pod_gpu: int = 0,
    schedulable: bool = True,
    schedulable_reason: str | None = None,
) -> RunnerEntry:
    return RunnerEntry(
        name=name,
        scale_set_name=f"c-mt-{name}",
        instance_type=instance_type,
        workflow_fleet=workflow_fleet,
        runner_class=runner_class,
        runner_pod_cpu_m=runner_pod_cpu_m,
        runner_pod_mem_mi=runner_pod_mem_mi,
        workflow_pod_cpu_m=workflow_pod_cpu_m,
        workflow_pod_mem_mi=workflow_pod_mem_mi,
        workflow_pod_gpu=workflow_pod_gpu,
        schedulable=schedulable,
        schedulable_reason=schedulable_reason,
    )


def _make_pool(
    name: str,
    *,
    fleet: str,
    instance_type: str,
    arch: str = "amd64",
    gpu: bool = False,
    runner_class: str | None = None,
) -> NodePoolEntry:
    return NodePoolEntry(
        name=name,
        fleet=fleet,
        instance_type=instance_type,
        arch=arch,
        gpu=gpu,
        runner_class=runner_class,
    )


def _make_topology(
    *,
    runners: list[RunnerEntry],
    nodepools: list[NodePoolEntry],
    runner_pool_fleet: str | None = "c7i-runner",
    workflow_pool_fleets: set[str] | None = None,
) -> ClusterTopology:
    fleets = workflow_pool_fleets or {p.fleet for p in nodepools if p.fleet != runner_pool_fleet}
    return ClusterTopology(
        cluster_id="test",
        region="us-east-2",
        modules=["nodepools", "arc-runners"],
        nodepools=nodepools,
        runner_pool_fleet=runner_pool_fleet,
        workflow_pool_fleets=fleets,
        runners=runners,
    )


# ---------------------------------------------------------------------------
# build_peak_targets — driven by PEAK_CONCURRENT snapshot
# ---------------------------------------------------------------------------


class TestBuildPeakTargets:
    def test_includes_only_schedulable(self):
        # Use a real runner name from PEAK_CONCURRENT so we actually get a count.
        good = _make_runner("l-x86iavx512-8-16", workflow_fleet="c7a")
        bad = _make_runner("l-x86iavx512-46-85", workflow_fleet="c7a", schedulable=False)
        targets = build_peak_targets([good, bad])
        assert "l-x86iavx512-8-16" in targets
        assert targets["l-x86iavx512-8-16"] > 0
        assert "l-x86iavx512-46-85" not in targets

    def test_unknown_new_label_skipped(self):
        runner = _make_runner("totally-unknown-label-not-in-snapshot")
        assert build_peak_targets([runner]) == {}

    def test_empty_runners(self):
        assert build_peak_targets([]) == {}

    def test_collapses_old_labels(self):
        # linux.2xlarge AND linux.c7i.2xlarge both map to l-x86iavx512-8-16.
        runner = _make_runner("l-x86iavx512-8-16", workflow_fleet="c7a")
        targets = build_peak_targets([runner])
        # Should be the SUM of both old labels' peaks (1473 + 927).
        assert targets["l-x86iavx512-8-16"] == 1473 + 927


# ---------------------------------------------------------------------------
# build_weighted_pool / weighted_mape
# ---------------------------------------------------------------------------


class TestBuildWeightedPool:
    def test_repeats_each_name_by_weight(self):
        pool = build_weighted_pool({"a": 2, "b": 3})
        assert pool.count("a") == 2
        assert pool.count("b") == 3

    def test_zero_weight_excluded(self):
        pool = build_weighted_pool({"a": 5, "b": 0})
        assert "b" not in pool
        assert pool.count("a") == 5

    def test_all_zero_weights(self):
        assert build_weighted_pool({"a": 0, "b": 0}) == []


class TestWeightedMape:
    def test_exact_match(self):
        assert weighted_mape({"a": 10, "b": 20}, {"a": 10, "b": 20}) == 0.0

    def test_partial_deployment(self):
        assert weighted_mape({"a": 50, "b": 50}, {"a": 100, "b": 100}) == 0.5

    def test_empty_targets(self):
        assert weighted_mape({}, {}) == 0.0

    def test_over_deployment(self):
        assert weighted_mape({"a": 15}, {"a": 10}) == 0.5

    def test_missing_runner_in_deployed(self):
        assert abs(weighted_mape({"a": 10}, {"a": 10, "b": 20}) - 20 / 30) < 1e-9


# ---------------------------------------------------------------------------
# SimNode capacity checks
# ---------------------------------------------------------------------------


class TestSimNode:
    def test_can_fit_when_capacity_available(self):
        node = SimNode("c7a.48xlarge", "c7a", None, cpu_m=10000, mem_mi=20000, gpu=0)
        assert node.can_fit(5000, 10000, 0) is True

    def test_does_not_fit_cpu(self):
        node = SimNode("c7a.48xlarge", "c7a", None, cpu_m=10000, mem_mi=20000, gpu=0, used_cpu_m=8000)
        assert node.can_fit(5000, 1000, 0) is False

    def test_does_not_fit_memory(self):
        node = SimNode("c7a.48xlarge", "c7a", None, cpu_m=10000, mem_mi=20000, gpu=0, used_mem_mi=18000)
        assert node.can_fit(1000, 5000, 0) is False

    def test_does_not_fit_gpu(self):
        node = SimNode("g5.8xlarge", "g5", None, cpu_m=10000, mem_mi=20000, gpu=1, used_gpu=1)
        assert node.can_fit(1000, 1000, 1) is False

    def test_remaining_capacity(self):
        node = SimNode(
            "c7a.48xlarge", "c7a", None, cpu_m=10000, mem_mi=20000, gpu=4,
            used_cpu_m=3000, used_mem_mi=5000, used_gpu=1,
        )  # fmt: skip
        assert node.remaining_cpu_m == 7000
        assert node.remaining_mem_mi == 15000
        assert node.remaining_gpu == 3

    def test_allocate_increments_pod_count(self):
        node = SimNode("c7a.48xlarge", "c7a", None, cpu_m=10000, mem_mi=20000, gpu=0)
        node.allocate(2000, 4000, 0)
        node.allocate(1000, 2000, 0)
        assert node.pod_count == 2
        assert node.used_cpu_m == 3000
        assert node.used_mem_mi == 6000


# ---------------------------------------------------------------------------
# best_fit_place_workflow
# ---------------------------------------------------------------------------


class TestBestFitPlaceWorkflow:
    def test_picks_tightest_matching_node(self):
        runner = _make_runner("r1", workflow_fleet="c7a", workflow_pod_cpu_m=2000, workflow_pod_mem_mi=4096)
        nodes = [
            SimNode("c7a.48xlarge", "c7a", None, cpu_m=20000, mem_mi=40000, gpu=0),
            SimNode("c7a.48xlarge", "c7a", None, cpu_m=20000, mem_mi=40000, gpu=0,
                    used_cpu_m=15000, used_mem_mi=30000),
        ]  # fmt: skip
        pick = best_fit_place_workflow(runner, nodes)
        assert pick is nodes[1]

    def test_returns_none_when_nothing_fits(self):
        runner = _make_runner("r1", workflow_fleet="c7a", workflow_pod_cpu_m=10000, workflow_pod_mem_mi=20000)
        nodes = [SimNode("c7a.48xlarge", "c7a", None, cpu_m=5000, mem_mi=10000, gpu=0)]
        assert best_fit_place_workflow(runner, nodes) is None

    def test_skips_wrong_fleet(self):
        runner = _make_runner("r1", workflow_fleet="c7a", workflow_pod_cpu_m=2000, workflow_pod_mem_mi=4096)
        nodes = [
            SimNode("m8g.48xlarge", "m8g", None, cpu_m=20000, mem_mi=40000, gpu=0),
            SimNode("c7a.48xlarge", "c7a", None, cpu_m=20000, mem_mi=40000, gpu=0),
        ]
        pick = best_fit_place_workflow(runner, nodes)
        assert pick is nodes[1]

    def test_skips_wrong_runner_class(self):
        runner = _make_runner(
            "rel", workflow_fleet="m8g", runner_class="release",
            workflow_pod_cpu_m=2000, workflow_pod_mem_mi=4096,
        )  # fmt: skip
        nodes = [
            SimNode("m8g.48xlarge", "m8g", None, cpu_m=20000, mem_mi=40000, gpu=0),
            SimNode("m8g.48xlarge", "m8g", "release", cpu_m=20000, mem_mi=40000, gpu=0),
        ]
        pick = best_fit_place_workflow(runner, nodes)
        assert pick is nodes[1]

    def test_empty_node_list(self):
        runner = _make_runner("r1", workflow_fleet="c7a")
        assert best_fit_place_workflow(runner, []) is None

    def test_gpu_scoring_prefers_tighter_gpu_fit(self):
        runner = _make_runner(
            "r1", workflow_fleet="g5", instance_type="g5.8xlarge",
            workflow_pod_cpu_m=1000, workflow_pod_mem_mi=1000, workflow_pod_gpu=1,
        )  # fmt: skip
        nodes = [
            SimNode("g5.8xlarge", "g5", None, cpu_m=10000, mem_mi=20000, gpu=4,
                    used_cpu_m=5000, used_mem_mi=10000, used_gpu=1),
            SimNode("g5.8xlarge", "g5", None, cpu_m=10000, mem_mi=20000, gpu=4,
                    used_cpu_m=5000, used_mem_mi=10000, used_gpu=3),
        ]  # fmt: skip
        pick = best_fit_place_workflow(runner, nodes)
        assert pick is nodes[1]


# ---------------------------------------------------------------------------
# best_fit_place_runner
# ---------------------------------------------------------------------------


class TestBestFitPlaceRunner:
    def test_picks_tightest_runner_node(self):
        nodes = [
            SimNode("c7i.48xlarge", "c7i-runner", None, cpu_m=190000, mem_mi=350000, gpu=0),
            SimNode("c7i.48xlarge", "c7i-runner", None, cpu_m=190000, mem_mi=350000, gpu=0,
                    used_cpu_m=180000, used_mem_mi=340000),
        ]  # fmt: skip
        pick = best_fit_place_runner("c7i-runner", 750, 512, nodes)
        assert pick is nodes[1]

    def test_returns_none_when_nothing_fits(self):
        nodes = [
            SimNode("c7i.48xlarge", "c7i-runner", None, cpu_m=1000, mem_mi=1000, gpu=0,
                    used_cpu_m=900, used_mem_mi=900),
        ]  # fmt: skip
        assert best_fit_place_runner("c7i-runner", 750, 512, nodes) is None

    def test_skips_wrong_fleet(self):
        nodes = [
            SimNode("m8g.48xlarge", "m8g", None, cpu_m=10000, mem_mi=20000, gpu=0),
            SimNode("c7i.48xlarge", "c7i-runner", None, cpu_m=10000, mem_mi=20000, gpu=0),
        ]
        pick = best_fit_place_runner("c7i-runner", 750, 512, nodes)
        assert pick is nodes[1]

    def test_empty_node_list(self):
        assert best_fit_place_runner("c7i-runner", 750, 512, []) is None


# ---------------------------------------------------------------------------
# provision_workflow_node / provision_runner_node
# ---------------------------------------------------------------------------


class TestProvisionWorkflowNode:
    def test_known_instance_type(self):
        runner = _make_runner("r1", instance_type="c7a.48xlarge", workflow_fleet="c7a")
        node = provision_workflow_node(runner, FAKE_DAEMONSETS)
        assert node is not None
        assert node.instance_type == "c7a.48xlarge"
        assert node.fleet == "c7a"
        assert node.runner_class is None
        assert node.cpu_m > 0
        assert node.mem_mi > 0
        assert node.gpu == 0

    def test_unknown_instance_type_returns_none(self):
        runner = _make_runner("r1", instance_type="z99.nonexistent", workflow_fleet="z99")
        assert provision_workflow_node(runner, FAKE_DAEMONSETS) is None

    def test_gpu_instance(self):
        runner = _make_runner("r1", instance_type="g5.8xlarge", workflow_fleet="g5", workflow_pod_gpu=1)
        node = provision_workflow_node(runner, FAKE_DAEMONSETS)
        assert node is not None
        assert node.gpu == 1

    def test_release_runner_class_propagates(self):
        runner = _make_runner("rel", instance_type="m8g.48xlarge", workflow_fleet="m8g", runner_class="release")
        node = provision_workflow_node(runner, FAKE_DAEMONSETS)
        assert node is not None
        assert node.runner_class == "release"


class TestProvisionRunnerNode:
    def test_picks_largest_instance(self):
        nodepools = [
            _make_pool("c7i-runner-24xlarge", fleet="c7i-runner", instance_type="c7i.24xlarge"),
            _make_pool("c7i-runner-48xlarge", fleet="c7i-runner", instance_type="c7i.48xlarge"),
        ]
        node = provision_runner_node("c7i-runner", nodepools, FAKE_DAEMONSETS)
        assert node is not None
        assert node.instance_type == "c7i.48xlarge"
        assert node.fleet == "c7i-runner"
        assert node.runner_class is None
        assert node.gpu == 0

    def test_no_matching_fleet_returns_none(self):
        nodepools = [_make_pool("m8g-48xlarge", fleet="m8g", instance_type="m8g.48xlarge")]
        assert provision_runner_node("c7i-runner", nodepools, FAKE_DAEMONSETS) is None

    def test_unknown_instance_type_returns_none(self):
        nodepools = [_make_pool("bogus", fleet="c7i-runner", instance_type="z99.nonexistent")]
        assert provision_runner_node("c7i-runner", nodepools, FAKE_DAEMONSETS) is None


# ---------------------------------------------------------------------------
# compute_utilization
# ---------------------------------------------------------------------------


class TestComputeUtilization:
    def test_workflow_only(self):
        wf = SimNode("c7a.48xlarge", "c7a", None, cpu_m=10000, mem_mi=20000, gpu=0,
                     used_cpu_m=8000, used_mem_mi=15000)  # fmt: skip
        result = SimResult(workflow_nodes=[wf], runner_nodes=[], deployed={"r1": 1}, targets={"r1": 1})
        util = compute_utilization(result)
        assert isinstance(util, SimulationUtilization)
        assert util.workflow.cpu_pct == 80.0
        assert util.workflow.mem_pct == 75.0
        assert util.workflow.nodes == 1
        assert util.runner.nodes == 0

    def test_both_pools(self):
        wf = SimNode("c7a.48xlarge", "c7a", None, cpu_m=10000, mem_mi=20000, gpu=0,
                     used_cpu_m=5000, used_mem_mi=10000)  # fmt: skip
        rn = SimNode("c7i.48xlarge", "c7i-runner", None, cpu_m=10000, mem_mi=20000, gpu=0,
                     used_cpu_m=2500, used_mem_mi=5000)  # fmt: skip
        result = SimResult(workflow_nodes=[wf], runner_nodes=[rn], deployed={"r1": 1}, targets={"r1": 1})
        util = compute_utilization(result)
        assert util.workflow.nodes == 1
        assert util.runner.nodes == 1
        assert util.workflow.cpu_pct == 50.0
        assert util.runner.cpu_pct == 25.0

    def test_gpu_pool(self):
        wf = SimNode("g5.8xlarge", "g5", None, cpu_m=10000, mem_mi=20000, gpu=1, used_gpu=1)
        result = SimResult(workflow_nodes=[wf], runner_nodes=[], deployed={"r1": 1}, targets={"r1": 1})
        util = compute_utilization(result)
        assert util.workflow.gpu_pct == 100.0
        assert util.workflow.total_gpu == 1

    def test_empty_result(self):
        util = compute_utilization(SimResult())
        assert util.workflow.nodes == 0
        assert util.runner.nodes == 0
        assert util.workflow.cpu_pct == 0.0
        assert util.runner.cpu_pct == 0.0


class TestPoolUtilizationProperties:
    def test_zero_total_returns_zero_pct(self):
        empty = PoolUtilization()
        assert empty.cpu_pct == 0.0
        assert empty.mem_pct == 0.0
        assert empty.gpu_pct == 0.0


# ---------------------------------------------------------------------------
# run_simulation
# ---------------------------------------------------------------------------


def _topology_one_runner(name: str, *, instance_type: str, workflow_fleet: str) -> ClusterTopology:
    runner = _make_runner(name, instance_type=instance_type, workflow_fleet=workflow_fleet)
    nodepools = [
        _make_pool("c7i-runner-48xlarge", fleet="c7i-runner", instance_type="c7i.48xlarge"),
        _make_pool(f"{workflow_fleet}-48xlarge", fleet=workflow_fleet, instance_type=instance_type),
    ]
    return _make_topology(runners=[runner], nodepools=nodepools)


class TestRunSimulation:
    def test_basic_completion_two_phase(self):
        topo = _topology_one_runner("l-x86iavx512-8-16", instance_type="c7a.48xlarge", workflow_fleet="c7a")
        targets = {"l-x86iavx512-8-16": 10}
        result = run_simulation(topo, targets, FAKE_DAEMONSETS, seed=42)
        assert sum(result.deployed.values()) > 0
        # Both pools should have nodes when runner_pool_fleet is set.
        assert len(result.workflow_nodes) > 0
        assert len(result.runner_nodes) > 0
        # Workflow nodes use the runner's workflow_fleet.
        assert all(n.fleet == "c7a" for n in result.workflow_nodes)
        # Runner nodes use the topology's runner pool.
        assert all(n.fleet == "c7i-runner" for n in result.runner_nodes)

    def test_no_runner_pool_skips_runner_phase(self):
        runner = _make_runner("r1", instance_type="c7a.48xlarge", workflow_fleet="c7a")
        nodepools = [_make_pool("c7a-48xlarge", fleet="c7a", instance_type="c7a.48xlarge")]
        topo = _make_topology(runners=[runner], nodepools=nodepools, runner_pool_fleet=None)
        result = run_simulation(topo, {"r1": 5}, FAKE_DAEMONSETS, seed=42)
        # Without a runner pool, only workflow nodes get provisioned.
        assert len(result.workflow_nodes) > 0
        assert result.runner_nodes == []
        assert sum(result.deployed.values()) > 0

    def test_deterministic_with_same_seed(self):
        topo = _topology_one_runner("l-x86iavx512-8-16", instance_type="c7a.48xlarge", workflow_fleet="c7a")
        targets = {"l-x86iavx512-8-16": 20}
        r1 = run_simulation(topo, targets, FAKE_DAEMONSETS, seed=99)
        r2 = run_simulation(topo, targets, FAKE_DAEMONSETS, seed=99)
        assert r1.deployed == r2.deployed
        assert len(r1.workflow_nodes) == len(r2.workflow_nodes)
        assert len(r1.runner_nodes) == len(r2.runner_nodes)

    def test_unschedulable_runner_is_skipped(self):
        good = _make_runner("good", instance_type="c7a.48xlarge", workflow_fleet="c7a")
        bad = _make_runner(
            "bad", instance_type="c7a.48xlarge", workflow_fleet="c7a",
            schedulable=False, schedulable_reason="no fleet",
        )  # fmt: skip
        nodepools = [
            _make_pool("c7i-runner-48xlarge", fleet="c7i-runner", instance_type="c7i.48xlarge"),
            _make_pool("c7a-48xlarge", fleet="c7a", instance_type="c7a.48xlarge"),
        ]
        topo = _make_topology(runners=[good, bad], nodepools=nodepools)
        result = run_simulation(topo, {"good": 5, "bad": 5}, FAKE_DAEMONSETS, seed=42)
        assert "bad" in result.skipped
        assert "bad" not in result.deployed
        assert result.deployed.get("good", 0) > 0

    def test_unknown_instance_type_does_not_infinite_loop(self):
        # One runner with a bogus instance type, one with a real type.
        good = _make_runner("good", instance_type="c7a.48xlarge", workflow_fleet="c7a")
        bad = _make_runner("bad", instance_type="z99.nonexistent", workflow_fleet="c7a")
        nodepools = [
            _make_pool("c7i-runner-48xlarge", fleet="c7i-runner", instance_type="c7i.48xlarge"),
            _make_pool("c7a-48xlarge", fleet="c7a", instance_type="c7a.48xlarge"),
        ]
        topo = _make_topology(runners=[good, bad], nodepools=nodepools)
        # Should complete without hanging — bad gets marked as failed after first attempt.
        result = run_simulation(topo, {"good": 5, "bad": 5}, FAKE_DAEMONSETS, seed=42)
        assert result.deployed.get("good", 0) > 0

    def test_all_unknown_instance_types_terminates(self):
        bad = _make_runner("bad", instance_type="z99.nonexistent", workflow_fleet="c7a")
        nodepools = [
            _make_pool("c7i-runner-48xlarge", fleet="c7i-runner", instance_type="c7i.48xlarge"),
            _make_pool("c7a-48xlarge", fleet="c7a", instance_type="c7a.48xlarge"),
        ]
        topo = _make_topology(runners=[bad], nodepools=nodepools)
        result = run_simulation(topo, {"bad": 10}, FAKE_DAEMONSETS, seed=42)
        # Loop must terminate even when no runner ever places.
        assert result.deployed.get("bad", 0) == 0

    def test_empty_targets(self):
        topo = _topology_one_runner("r1", instance_type="c7a.48xlarge", workflow_fleet="c7a")
        result = run_simulation(topo, {}, FAKE_DAEMONSETS, seed=42)
        assert result.workflow_nodes == []
        assert result.runner_nodes == []

    def test_zero_weight_targets(self):
        topo = _topology_one_runner("r1", instance_type="c7a.48xlarge", workflow_fleet="c7a")
        result = run_simulation(topo, {"r1": 0}, FAKE_DAEMONSETS, seed=42)
        assert sum(result.deployed.values()) == 0

    def test_release_workflow_node_runner_class_propagates(self):
        # Release runner — workflow nodes must carry runner_class="release".
        rel = _make_runner(
            "rel", instance_type="m8g.48xlarge", workflow_fleet="m8g",
            runner_class="release",
        )  # fmt: skip
        nodepools = [
            _make_pool("c7i-runner-48xlarge", fleet="c7i-runner", instance_type="c7i.48xlarge"),
            _make_pool("m8g-48xlarge-release", fleet="m8g", instance_type="m8g.48xlarge", runner_class="release"),
        ]
        topo = _make_topology(runners=[rel], nodepools=nodepools)
        result = run_simulation(topo, {"rel": 3}, FAKE_DAEMONSETS, seed=42)
        # All workflow nodes belong to the release class.
        assert all(n.runner_class == "release" for n in result.workflow_nodes)
        # Runner nodes never carry a runner_class — runner pool is shared.
        assert all(n.runner_class is None for n in result.runner_nodes)
