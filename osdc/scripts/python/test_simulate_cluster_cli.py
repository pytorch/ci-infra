"""Tests for simulate_cluster_cli module (split-pool model)."""

from __future__ import annotations

from unittest.mock import patch

from cluster_topology import ClusterTopology, NodePoolEntry, RunnerEntry
from daemonset_overhead import DaemonSetOverhead
from simulate_cluster import (
    PoolUtilization,
    SimNode,
    SimResult,
    SimulationUtilization,
)
from simulate_cluster_cli import (
    _format_pool,
    _percentile,
    _print_deployment_accuracy,
    _print_multi,
    _print_node_breakdown,
    _print_results,
    _resolve_roots,
    main,
    parse_args,
)

FAKE_DS = [DaemonSetOverhead("kube-proxy", 50, 80, False, "test")]


def _runner(name: str = "r1", **overrides) -> RunnerEntry:
    base = {
        "name": name,
        "scale_set_name": f"c-mt-{name}",
        "instance_type": "c7a.48xlarge",
        "workflow_fleet": "c7a",
        "runner_class": None,
        "runner_pod_cpu_m": 750,
        "runner_pod_mem_mi": 512,
        "workflow_pod_cpu_m": 8000,
        "workflow_pod_mem_mi": 16 * 1024,
        "workflow_pod_gpu": 0,
        "schedulable": True,
        "schedulable_reason": None,
    }
    base.update(overrides)
    return RunnerEntry(**base)


def _topology(*, runner_pool_fleet: str | None = "c7i-runner") -> ClusterTopology:
    runners = [_runner("r1")]
    nodepools = [
        NodePoolEntry("c7i-runner-48xlarge", "c7i-runner", "c7i.48xlarge", "amd64", False, None),
        NodePoolEntry("c7a-48xlarge", "c7a", "c7a.48xlarge", "amd64", False, None),
    ]
    workflow_fleets = {"c7a"}
    return ClusterTopology(
        cluster_id="test",
        region="us-east-2",
        modules=["nodepools", "arc-runners"],
        nodepools=nodepools,
        runner_pool_fleet=runner_pool_fleet,
        workflow_pool_fleets=workflow_fleets,
        runners=runners,
    )


def _make_result() -> SimResult:
    wf = SimNode("c7a.48xlarge", "c7a", None, cpu_m=10000, mem_mi=20000, gpu=0,
                 used_cpu_m=8000, used_mem_mi=15000, pod_count=2)  # fmt: skip
    rn = SimNode("c7i.48xlarge", "c7i-runner", None, cpu_m=10000, mem_mi=20000, gpu=0,
                 used_cpu_m=2000, used_mem_mi=3000, pod_count=2)  # fmt: skip
    return SimResult(
        workflow_nodes=[wf],
        runner_nodes=[rn],
        deployed={"r1": 2},
        targets={"r1": 2},
        skipped=["legacy-name"],
    )


def _make_util() -> SimulationUtilization:
    wf = PoolUtilization(nodes=1, used_cpu_m=8000, total_cpu_m=10000,
                         used_mem_mi=15000, total_mem_mi=20000)  # fmt: skip
    rn = PoolUtilization(nodes=1, used_cpu_m=2000, total_cpu_m=10000,
                         used_mem_mi=3000, total_mem_mi=20000)  # fmt: skip
    return SimulationUtilization(workflow=wf, runner=rn)


# ---------------------------------------------------------------------------
# parse_args / _resolve_roots
# ---------------------------------------------------------------------------


class TestParseArgs:
    def test_required_cluster(self):
        args = parse_args(["--cluster", "arc-staging"])
        assert args.cluster == "arc-staging"
        assert args.seed == 42
        assert args.threshold == 0.15
        assert args.rounds == 1

    def test_overrides(self):
        args = parse_args(["--cluster", "x", "--seed", "7", "--threshold", "0.05", "--rounds", "5"])
        assert args.seed == 7
        assert args.threshold == 0.05
        assert args.rounds == 5

    def test_missing_cluster_errors(self):
        import pytest

        with pytest.raises(SystemExit):
            parse_args([])


class TestResolveRoots:
    def test_env_vars_take_precedence(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OSDC_UPSTREAM", str(tmp_path))
        monkeypatch.setenv("OSDC_ROOT", str(tmp_path))
        upstream, consumer = _resolve_roots()
        assert upstream == tmp_path.resolve()
        assert consumer == tmp_path.resolve()

    def test_root_falls_back_to_upstream(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OSDC_UPSTREAM", str(tmp_path))
        monkeypatch.delenv("OSDC_ROOT", raising=False)
        upstream, consumer = _resolve_roots()
        assert upstream == tmp_path.resolve()
        assert consumer == tmp_path.resolve()


# ---------------------------------------------------------------------------
# _percentile
# ---------------------------------------------------------------------------


class TestPercentile:
    def test_empty_list(self):
        assert _percentile([], 50) == 0.0

    def test_single_value(self):
        assert _percentile([42.0], 50) == 42.0

    def test_median(self):
        assert _percentile([1.0, 2.0, 3.0, 4.0, 5.0], 50) == 3.0

    def test_zero_percentile(self):
        assert _percentile([1.0, 2.0, 3.0], 0) == 1.0

    def test_hundredth_percentile(self):
        assert _percentile([1.0, 2.0, 3.0], 100) == 3.0


# ---------------------------------------------------------------------------
# Output formatters
# ---------------------------------------------------------------------------


class TestFormatPool:
    def test_renders_pool_with_nodes(self, capsys):
        util = PoolUtilization(nodes=2, used_cpu_m=8000, total_cpu_m=10000,
                               used_mem_mi=15000, total_mem_mi=20000,
                               used_gpu=1, total_gpu=2)  # fmt: skip
        _format_pool(util, "Workflow Pool")
        out = capsys.readouterr().out
        assert "Workflow Pool" in out
        assert "vCPU" in out
        assert "Memory" in out
        assert "GPU" in out

    def test_no_nodes_renders_placeholder(self, capsys):
        _format_pool(PoolUtilization(), "Runner Pool")
        out = capsys.readouterr().out
        assert "Runner Pool" in out
        assert "no nodes provisioned" in out

    def test_no_gpu_section_when_total_gpu_zero(self, capsys):
        util = PoolUtilization(nodes=1, used_cpu_m=5000, total_cpu_m=10000,
                               used_mem_mi=5000, total_mem_mi=10000)  # fmt: skip
        _format_pool(util, "Workflow Pool")
        out = capsys.readouterr().out
        assert "GPU:" not in out


class TestPrintNodeBreakdown:
    def test_groups_by_fleet(self, capsys):
        result = _make_result()
        _print_node_breakdown(result)
        out = capsys.readouterr().out
        assert "c7a" in out
        assert "c7i-runner" in out

    def test_empty_result_prints_nothing_extra(self, capsys):
        empty = SimResult()
        _print_node_breakdown(empty)
        out = capsys.readouterr().out
        # Should produce no output when there are no nodes.
        assert "Nodes by fleet" not in out


class TestPrintDeploymentAccuracy:
    def test_shows_deployed_vs_target(self, capsys):
        _print_deployment_accuracy(_make_result())
        out = capsys.readouterr().out
        assert "Deployment accuracy" in out
        assert "r1" in out

    def test_zero_targets_skips_breakdown(self, capsys):
        _print_deployment_accuracy(SimResult())
        out = capsys.readouterr().out
        assert "Deployment accuracy" in out
        # No per-runner table when targets is empty.
        assert "Runner" not in out.split("Total deployed")[1]


class TestPrintResults:
    def test_renders_complete_summary(self, capsys):
        _print_results(_make_result(), _make_util())
        out = capsys.readouterr().out
        assert "Cluster Simulation Results" in out
        assert "Workflow Pool" in out
        assert "Runner Pool" in out
        assert "Skipped runner targets" in out
        assert "legacy-name" in out

    def test_no_skipped_section_when_empty(self, capsys):
        result = _make_result()
        result.skipped = []
        _print_results(result, _make_util())
        out = capsys.readouterr().out
        assert "Skipped runner targets" not in out


class TestPrintMulti:
    def test_renders_multi_round_summary(self, capsys):
        utils = [_make_util(), _make_util()]
        _print_multi(utils, seed=42, rounds=2)
        out = capsys.readouterr().out
        assert "Multi-Round Summary" in out
        assert "Workflow Pool" in out
        assert "Runner Pool" in out

    def test_no_nodes_message(self, capsys):
        empty_pool = PoolUtilization()
        utils = [SimulationUtilization(workflow=empty_pool, runner=empty_pool)]
        _print_multi(utils, seed=42, rounds=1)
        out = capsys.readouterr().out
        assert "no nodes provisioned across rounds" in out


# ---------------------------------------------------------------------------
# main — smoke test with mocked resolve_cluster + discover_daemonsets
# ---------------------------------------------------------------------------


class TestMainSmoke:
    @patch("simulate_cluster_cli.resolve_cluster")
    @patch("simulate_cluster_cli.discover_daemonsets")
    def test_single_run(self, mock_ds, mock_resolve, capsys, tmp_path, monkeypatch):
        monkeypatch.setenv("OSDC_UPSTREAM", str(tmp_path))
        monkeypatch.setenv("OSDC_ROOT", str(tmp_path))
        mock_ds.return_value = FAKE_DS
        # Use a runner whose new label matches a real PEAK_CONCURRENT entry.
        mock_resolve.return_value = ClusterTopology(
            cluster_id="test",
            region="us-east-2",
            modules=["nodepools", "arc-runners"],
            nodepools=[
                NodePoolEntry("c7i-runner-48xlarge", "c7i-runner", "c7i.48xlarge", "amd64", False, None),
                NodePoolEntry("c7a-48xlarge", "c7a", "c7a.48xlarge", "amd64", False, None),
            ],
            runner_pool_fleet="c7i-runner",
            workflow_pool_fleets={"c7a"},
            runners=[_runner("l-x86iavx512-8-16", workflow_fleet="c7a")],
        )
        result = main(["--cluster", "test"])
        assert result == 0
        out = capsys.readouterr().out
        assert "Cluster Simulation Results" in out

    @patch("simulate_cluster_cli.resolve_cluster")
    @patch("simulate_cluster_cli.discover_daemonsets")
    def test_multi_round(self, mock_ds, mock_resolve, capsys, tmp_path, monkeypatch):
        monkeypatch.setenv("OSDC_UPSTREAM", str(tmp_path))
        monkeypatch.setenv("OSDC_ROOT", str(tmp_path))
        mock_ds.return_value = FAKE_DS
        mock_resolve.return_value = ClusterTopology(
            cluster_id="test",
            region="us-east-2",
            modules=["nodepools", "arc-runners"],
            nodepools=[
                NodePoolEntry("c7i-runner-48xlarge", "c7i-runner", "c7i.48xlarge", "amd64", False, None),
                NodePoolEntry("c7a-48xlarge", "c7a", "c7a.48xlarge", "amd64", False, None),
            ],
            runner_pool_fleet="c7i-runner",
            workflow_pool_fleets={"c7a"},
            runners=[_runner("l-x86iavx512-8-16", workflow_fleet="c7a")],
        )
        result = main(["--cluster", "test", "--rounds", "2"])
        assert result == 0
        out = capsys.readouterr().out
        assert "Multi-Round Summary" in out

    @patch("simulate_cluster_cli.resolve_cluster")
    @patch("simulate_cluster_cli.discover_daemonsets")
    def test_no_targets_returns_1(self, mock_ds, mock_resolve, capsys, tmp_path, monkeypatch):
        monkeypatch.setenv("OSDC_UPSTREAM", str(tmp_path))
        monkeypatch.setenv("OSDC_ROOT", str(tmp_path))
        mock_ds.return_value = FAKE_DS
        # Topology with no runners → no targets → return 1.
        mock_resolve.return_value = ClusterTopology(
            cluster_id="test",
            region="us-east-2",
            modules=["nodepools"],
            nodepools=[],
            runner_pool_fleet=None,
            workflow_pool_fleets=set(),
            runners=[],
        )
        result = main(["--cluster", "test"])
        assert result == 1

    @patch("simulate_cluster_cli.resolve_cluster")
    @patch("simulate_cluster_cli.discover_daemonsets")
    def test_staging_warning(self, mock_ds, mock_resolve, capsys, tmp_path, monkeypatch):
        monkeypatch.setenv("OSDC_UPSTREAM", str(tmp_path))
        monkeypatch.setenv("OSDC_ROOT", str(tmp_path))
        mock_ds.return_value = FAKE_DS
        mock_resolve.return_value = ClusterTopology(
            cluster_id="arc-staging",
            region="us-west-1",
            modules=["nodepools", "arc-runners"],
            nodepools=[
                NodePoolEntry("c7i-runner-48xlarge", "c7i-runner", "c7i.48xlarge", "amd64", False, None),
                NodePoolEntry("c7a-48xlarge", "c7a", "c7a.48xlarge", "amd64", False, None),
            ],
            runner_pool_fleet="c7i-runner",
            workflow_pool_fleets={"c7a"},
            runners=[_runner("l-x86iavx512-8-16", workflow_fleet="c7a")],
        )
        result = main(["--cluster", "arc-staging"])
        assert result == 0
        out = capsys.readouterr().out
        assert "WARNING" in out
        assert "PEAK_CONCURRENT" in out
