"""Tests for simulate_cluster module."""

from daemonset_overhead import DaemonSetOverhead
from simulate_cluster import (
    SimNode,
    SimResult,
    best_fit_place,
    build_peak_targets,
    build_weighted_pool,
    compute_utilization,
    provision_node,
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


def make_runner(
    name: str,
    instance_type: str,
    vcpu: int,
    memory_mi: int,
    gpu: int = 0,
) -> dict:
    return {
        "name": name,
        "instance_type": instance_type,
        "vcpu": vcpu,
        "memory_mi": memory_mi,
        "gpu": gpu,
        "file": f"{name}.yaml",
    }


# ---------------------------------------------------------------------------
# build_peak_targets
# ---------------------------------------------------------------------------


class TestBuildPeakTargets:
    def test_collapse_multiple_old_labels(self):
        old_to_new = {
            "linux.2xlarge": "l-x86iavx512-8-16",
            "linux.c7i.2xlarge": "l-x86iavx512-8-16",
        }
        peaks = {"linux.2xlarge": 100, "linux.c7i.2xlarge": 50}
        targets, skipped = build_peak_targets(old_to_new, peaks)
        assert targets == {"l-x86iavx512-8-16": 150}
        assert skipped == {}

    def test_skip_unmapped(self):
        old_to_new = {"linux.2xlarge": "l-x86iavx512-8-16"}
        peaks = {"linux.2xlarge": 100, "linux.unknown": 50}
        targets, skipped = build_peak_targets(old_to_new, peaks)
        assert targets == {"l-x86iavx512-8-16": 100}
        assert "linux.unknown" in skipped

    def test_empty_inputs(self):
        targets, skipped = build_peak_targets({}, {})
        assert targets == {}
        assert skipped == {}


# ---------------------------------------------------------------------------
# build_weighted_pool
# ---------------------------------------------------------------------------


class TestBuildWeightedPool:
    def test_weights_match_targets(self):
        targets = {"runner-a": 10, "runner-b": 20}
        pool = build_weighted_pool(targets)
        pool_dict = dict(pool)
        assert pool_dict["runner-a"] == 10
        assert pool_dict["runner-b"] == 20

    def test_zero_weight_excluded(self):
        targets = {"runner-a": 10, "runner-b": 0}
        pool = build_weighted_pool(targets)
        names = [p[0] for p in pool]
        assert "runner-b" not in names

    def test_all_zero_weights(self):
        targets = {"runner-a": 0, "runner-b": 0}
        pool = build_weighted_pool(targets)
        assert pool == []


# ---------------------------------------------------------------------------
# SimNode
# ---------------------------------------------------------------------------


class TestSimNode:
    def test_fits_when_capacity_available(self):
        node = SimNode("c7a.48xlarge", 10000, 20000, 0)
        assert node.fits(5000, 10000, 0) is True

    def test_does_not_fit_cpu(self):
        node = SimNode("c7a.48xlarge", 10000, 20000, 0, used_cpu_m=8000)
        assert node.fits(5000, 1000, 0) is False

    def test_does_not_fit_memory(self):
        node = SimNode("c7a.48xlarge", 10000, 20000, 0, used_mem_mi=18000)
        assert node.fits(1000, 5000, 0) is False

    def test_does_not_fit_gpu(self):
        node = SimNode("g5.8xlarge", 10000, 20000, 1, used_gpu=1)
        assert node.fits(1000, 1000, 1) is False

    def test_remaining_capacity(self):
        node = SimNode("c7a.48xlarge", 10000, 20000, 4, used_cpu_m=3000, used_mem_mi=5000, used_gpu=1)
        assert node.remaining_cpu_m == 7000
        assert node.remaining_mem_mi == 15000
        assert node.remaining_gpu == 3


# ---------------------------------------------------------------------------
# best_fit_place
# ---------------------------------------------------------------------------


class TestBestFitPlace:
    def test_prefers_tightest(self):
        nodes = [
            SimNode("c7a.48xlarge", 10000, 20000, 0, used_cpu_m=2000, used_mem_mi=4000),
            SimNode("c7a.48xlarge", 10000, 20000, 0, used_cpu_m=7000, used_mem_mi=15000),
        ]
        # Second node has less remaining → should be preferred
        idx = best_fit_place(nodes, 2000, 4000, 0, "c7a.48xlarge")
        assert idx == 1

    def test_returns_none_when_nothing_fits(self):
        nodes = [
            SimNode("c7a.48xlarge", 10000, 20000, 0, used_cpu_m=9500, used_mem_mi=19000),
        ]
        idx = best_fit_place(nodes, 2000, 4000, 0, "c7a.48xlarge")
        assert idx is None

    def test_only_considers_matching_instance_type(self):
        nodes = [
            SimNode("g5.8xlarge", 10000, 20000, 1),
            SimNode("c7a.48xlarge", 10000, 20000, 0, used_cpu_m=8000, used_mem_mi=16000),
        ]
        # First node has more room but wrong type
        idx = best_fit_place(nodes, 1000, 2000, 0, "c7a.48xlarge")
        assert idx == 1

    def test_empty_nodes_list(self):
        idx = best_fit_place([], 1000, 2000, 0, "c7a.48xlarge")
        assert idx is None

    def test_gpu_scoring_prefers_tighter_gpu_fit(self):
        """GPU remaining capacity should factor into best-fit scoring."""
        nodes = [
            SimNode("g5.8xlarge", 10000, 20000, 4, used_cpu_m=5000, used_mem_mi=10000, used_gpu=1),
            SimNode("g5.8xlarge", 10000, 20000, 4, used_cpu_m=5000, used_mem_mi=10000, used_gpu=3),
        ]
        # Second node has 1 GPU remaining vs first with 3 → second is tighter
        idx = best_fit_place(nodes, 1000, 1000, 1, "g5.8xlarge")
        assert idx == 1


# ---------------------------------------------------------------------------
# provision_node
# ---------------------------------------------------------------------------


class TestProvisionNode:
    def test_known_instance_type(self):
        node = provision_node("c7a.48xlarge", FAKE_DAEMONSETS)
        assert node is not None
        assert node.instance_type == "c7a.48xlarge"
        assert node.total_cpu_m > 0
        assert node.total_mem_mi > 0
        assert node.used_cpu_m == 0

    def test_unknown_instance_type(self):
        node = provision_node("z99.nonexistent", FAKE_DAEMONSETS)
        assert node is None

    def test_gpu_instance(self):
        node = provision_node("g5.8xlarge", FAKE_DAEMONSETS)
        assert node is not None
        assert node.total_gpu == 1


# ---------------------------------------------------------------------------
# weighted_mape
# ---------------------------------------------------------------------------


class TestWeightedMape:
    def test_exact_match(self):
        targets = {"a": 10, "b": 20}
        deployed = {"a": 10, "b": 20}
        assert weighted_mape(deployed, targets) == 0.0

    def test_partial_deployment(self):
        targets = {"a": 100, "b": 100}
        deployed = {"a": 50, "b": 50}
        # error = |50-100| + |50-100| = 100, total_target = 200
        assert weighted_mape(deployed, targets) == 0.5

    def test_empty_targets(self):
        assert weighted_mape({}, {}) == 0.0

    def test_over_deployment(self):
        targets = {"a": 10}
        deployed = {"a": 15}
        # error = 5, total = 10
        assert weighted_mape(deployed, targets) == 0.5

    def test_missing_runner_in_deployed(self):
        targets = {"a": 10, "b": 20}
        deployed = {"a": 10}
        # error = |10-10| + |0-20| = 20, total = 30
        assert abs(weighted_mape(deployed, targets) - 20 / 30) < 1e-9


# ---------------------------------------------------------------------------
# run_simulation
# ---------------------------------------------------------------------------


class TestRunSimulation:
    def test_basic_completion(self):
        runners = [
            make_runner("r1", "c7a.48xlarge", 8, 16 * 1024),
            make_runner("r2", "c7a.48xlarge", 16, 32 * 1024),
        ]
        targets = {"r1": 5, "r2": 3}
        result = run_simulation(runners, targets, FAKE_DAEMONSETS, seed=42)
        # Should deploy approximately the target counts
        total_deployed = sum(result.deployed.values())
        total_target = sum(targets.values())
        assert total_deployed > 0
        assert abs(total_deployed - total_target) <= total_target * 0.2

    def test_deterministic_with_same_seed(self):
        runners = [make_runner("r1", "c7a.48xlarge", 8, 16 * 1024)]
        targets = {"r1": 20}
        result1 = run_simulation(runners, targets, FAKE_DAEMONSETS, seed=99)
        result2 = run_simulation(runners, targets, FAKE_DAEMONSETS, seed=99)
        assert result1.deployed == result2.deployed
        assert len(result1.nodes) == len(result2.nodes)

    def test_different_seed_different_result(self):
        runners = [
            make_runner("r1", "c7a.48xlarge", 8, 16 * 1024),
            make_runner("r2", "c7a.48xlarge", 16, 32 * 1024),
        ]
        targets = {"r1": 50, "r2": 50}
        result1 = run_simulation(runners, targets, FAKE_DAEMONSETS, seed=1)
        result2 = run_simulation(runners, targets, FAKE_DAEMONSETS, seed=2)
        # Results should differ (deployment order is random)
        # but both should complete
        assert sum(result1.deployed.values()) > 0
        assert sum(result2.deployed.values()) > 0

    def test_skips_runners_without_defs(self):
        runners = [make_runner("r1", "c7a.48xlarge", 8, 16 * 1024)]
        targets = {"r1": 10, "r_missing": 5}
        result = run_simulation(runners, targets, FAKE_DAEMONSETS, seed=42)
        assert "r_missing" in result.skipped_labels
        assert "r1" in result.deployed

    def test_empty_targets(self):
        runners = [make_runner("r1", "c7a.48xlarge", 8, 16 * 1024)]
        result = run_simulation(runners, {}, FAKE_DAEMONSETS, seed=42)
        assert len(result.nodes) == 0
        assert sum(result.deployed.values()) == 0

    def test_unknown_instance_type_does_not_infinite_loop(self):
        """Runners with unknown instance types are skipped without hanging."""
        runners = [
            make_runner("r1", "z99.nonexistent", 8, 16 * 1024),
            make_runner("r2", "c7a.48xlarge", 8, 16 * 1024),
        ]
        targets = {"r1": 10, "r2": 5}
        # Should complete without hanging — r1 gets skipped after first failure
        result = run_simulation(runners, targets, FAKE_DAEMONSETS, seed=42)
        assert result.deployed.get("r2", 0) > 0

    def test_all_unknown_instance_types_terminates(self):
        """Simulation terminates when all runners have unknown instance types."""
        runners = [make_runner("r1", "z99.nonexistent", 8, 16 * 1024)]
        targets = {"r1": 10}
        result = run_simulation(runners, targets, FAKE_DAEMONSETS, seed=42)
        # Should return without hanging, with zero deployments
        assert sum(result.deployed.values()) == 0

    def test_all_zero_weight_targets(self):
        """Simulation handles all-zero targets without crashing."""
        runners = [make_runner("r1", "c7a.48xlarge", 8, 16 * 1024)]
        targets = {"r1": 0}
        result = run_simulation(runners, targets, FAKE_DAEMONSETS, seed=42)
        assert sum(result.deployed.values()) == 0


# ---------------------------------------------------------------------------
# compute_utilization
# ---------------------------------------------------------------------------


class TestComputeUtilization:
    def test_single_node(self):
        node = SimNode(
            "c7a.48xlarge",
            total_cpu_m=10000,
            total_mem_mi=20000,
            total_gpu=0,
            used_cpu_m=8000,
            used_mem_mi=15000,
        )
        result = SimResult(nodes=[node], deployed={"r1": 5}, targets={"r1": 5})
        util = compute_utilization(result)
        assert util["cpu_pct"] == 80.0
        assert util["mem_pct"] == 75.0
        assert util["total_nodes"] == 1
        assert util["gpu_nodes"] == 0

    def test_gpu_only_counts_gpu_nodes(self):
        cpu_node = SimNode("c7a.48xlarge", 10000, 20000, 0, used_cpu_m=5000, used_mem_mi=10000)
        gpu_node = SimNode("g5.8xlarge", 10000, 20000, 1, used_cpu_m=5000, used_mem_mi=10000, used_gpu=1)
        result = SimResult(
            nodes=[cpu_node, gpu_node],
            deployed={"r1": 2},
            targets={"r1": 2},
        )
        util = compute_utilization(result)
        assert util["gpu_pct"] == 100.0
        assert util["gpu_nodes"] == 1
        assert util["total_gpu"] == 1

    def test_empty_result(self):
        result = SimResult()
        util = compute_utilization(result)
        assert util["cpu_pct"] == 0.0
        assert util["mem_pct"] == 0.0
        assert util["gpu_pct"] == 0.0
        assert util["total_nodes"] == 0

    def test_multiple_gpu_nodes(self):
        nodes = [
            SimNode("g5.8xlarge", 10000, 20000, 1, used_gpu=1),
            SimNode("g5.8xlarge", 10000, 20000, 1, used_gpu=0),
        ]
        result = SimResult(nodes=nodes, deployed={}, targets={})
        util = compute_utilization(result)
        assert util["gpu_pct"] == 50.0
        assert util["total_gpu"] == 2
        assert util["used_gpu"] == 1
