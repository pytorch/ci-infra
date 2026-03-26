"""Tests for simulate_cluster_cli module."""

from unittest.mock import MagicMock, patch

from daemonset_overhead import DaemonSetOverhead
from simulate_cluster import SimNode, SimResult
from simulate_cluster_cli import (
    _percentile,
    _print_deployment_accuracy,
    _print_multi_summary,
    _print_node_table,
    _print_utilization,
    _run_multi,
    main,
    print_results,
)

FAKE_DS = [
    DaemonSetOverhead("kube-proxy", 50, 80, False, "test"),
]


def _make_result():
    """Create a minimal SimResult for testing output functions."""
    node = SimNode(
        "c7a.48xlarge",
        total_cpu_m=10000,
        total_mem_mi=20000,
        total_gpu=0,
        used_cpu_m=8000,
        used_mem_mi=15000,
    )
    gpu_node = SimNode(
        "g5.8xlarge",
        total_cpu_m=10000,
        total_mem_mi=20000,
        total_gpu=1,
        used_cpu_m=5000,
        used_mem_mi=10000,
        used_gpu=1,
    )
    return SimResult(
        nodes=[node, gpu_node],
        deployed={"r1": 5, "r2": 3},
        targets={"r1": 5, "r2": 4},
        skipped_labels={"old-label": "no mapping"},
    )


def _make_utilization():
    return {
        "cpu_pct": 65.0,
        "mem_pct": 62.5,
        "gpu_pct": 100.0,
        "total_cpu_m": 20000,
        "used_cpu_m": 13000,
        "total_mem_mi": 40000,
        "used_mem_mi": 25000,
        "total_gpu": 1,
        "used_gpu": 1,
        "total_nodes": 2,
        "gpu_nodes": 1,
    }


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
# Output formatting functions (smoke tests)
# ---------------------------------------------------------------------------


class TestPrintResults:
    def test_prints_without_error(self, capsys):
        result = _make_result()
        utilization = _make_utilization()
        print_results(result, utilization)
        output = capsys.readouterr().out
        assert "Simulation Results" in output
        assert "r1" in output

    def test_no_skipped_labels(self, capsys):
        result = _make_result()
        result.skipped_labels = {}
        utilization = _make_utilization()
        print_results(result, utilization)
        output = capsys.readouterr().out
        assert "Skipped labels" not in output


class TestPrintNodeTable:
    def test_shows_instance_types(self, capsys):
        result = _make_result()
        _print_node_table(result)
        output = capsys.readouterr().out
        assert "c7a.48xlarge" in output
        assert "g5.8xlarge" in output


class TestPrintDeploymentAccuracy:
    def test_shows_deployed_vs_target(self, capsys):
        result = _make_result()
        _print_deployment_accuracy(result)
        output = capsys.readouterr().out
        assert "Deployment accuracy" in output
        assert "r1" in output
        assert "r2" in output


class TestPrintUtilization:
    def test_shows_cpu_and_mem(self, capsys):
        utilization = _make_utilization()
        _print_utilization(utilization)
        output = capsys.readouterr().out
        assert "vCPU" in output
        assert "Memory" in output
        assert "GPU" in output

    def test_no_gpu(self, capsys):
        utilization = _make_utilization()
        utilization["total_gpu"] = 0
        _print_utilization(utilization)
        output = capsys.readouterr().out
        assert "GPU:" not in output


class TestPrintMultiSummary:
    def test_prints_summary(self, capsys):
        utils = [
            {
                "cpu_pct": 80.0,
                "mem_pct": 75.0,
                "gpu_pct": 100.0,
                "total_gpu": 1,
                "total_nodes": 10,
            },
            {
                "cpu_pct": 85.0,
                "mem_pct": 78.0,
                "gpu_pct": 90.0,
                "total_gpu": 1,
                "total_nodes": 11,
            },
        ]
        _print_multi_summary(utils)
        output = capsys.readouterr().out
        assert "vCPU" in output
        assert "Nodes" in output

    def test_no_gpu_nodes(self, capsys):
        utils = [
            {
                "cpu_pct": 80.0,
                "mem_pct": 75.0,
                "gpu_pct": 0.0,
                "total_gpu": 0,
                "total_nodes": 5,
            },
        ]
        _print_multi_summary(utils)
        output = capsys.readouterr().out
        assert "GPU" not in output


# ---------------------------------------------------------------------------
# _run_multi
# ---------------------------------------------------------------------------


class TestRunMulti:
    @patch("simulate_cluster_cli.run_simulation")
    @patch("simulate_cluster_cli.compute_utilization")
    def test_runs_multiple_rounds(self, mock_util, mock_sim, capsys):
        mock_sim.return_value = SimResult(
            nodes=[SimNode("c7a.48xlarge", 10000, 20000, 0, used_cpu_m=8000, used_mem_mi=15000)],
            deployed={"r1": 5},
            targets={"r1": 5},
        )
        mock_util.return_value = {
            "cpu_pct": 80.0,
            "mem_pct": 75.0,
            "gpu_pct": 0.0,
            "total_gpu": 0,
            "total_nodes": 1,
        }
        runners = [{"name": "r1", "instance_type": "c7a.48xlarge", "vcpu": 8, "memory_mi": 16384, "gpu": 0}]
        args = MagicMock(seed=42, rounds=3, threshold=0.15)
        result = _run_multi(runners, {"r1": 5}, FAKE_DS, {}, args)
        assert result == 0
        assert mock_sim.call_count == 3

    @patch("simulate_cluster_cli.run_simulation")
    @patch("simulate_cluster_cli.compute_utilization")
    def test_run_multi_with_skipped_mapping(self, mock_util, mock_sim, capsys):
        """_run_multi prints skipped mapping when non-empty (line 133)."""
        mock_sim.return_value = SimResult(
            nodes=[SimNode("c7a.48xlarge", 10000, 20000, 0, used_cpu_m=8000, used_mem_mi=15000)],
            deployed={"r1": 5},
            targets={"r1": 5},
        )
        mock_util.return_value = {
            "cpu_pct": 80.0,
            "mem_pct": 75.0,
            "gpu_pct": 0.0,
            "total_gpu": 0,
            "total_nodes": 1,
        }
        runners = [{"name": "r1", "instance_type": "c7a.48xlarge", "vcpu": 8, "memory_mi": 16384, "gpu": 0}]
        args = MagicMock(seed=42, rounds=2, threshold=0.15)
        skipped = {"old-label": "no mapping found"}
        result = _run_multi(runners, {"r1": 5}, FAKE_DS, skipped, args)
        assert result == 0
        output = capsys.readouterr().out
        assert "Unmapped old labels" in output


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


class TestMain:
    @patch("simulate_cluster_cli.run_simulation")
    @patch("simulate_cluster_cli.compute_utilization")
    @patch("simulate_cluster_cli.load_runner_defs")
    @patch("simulate_cluster_cli.discover_daemonsets")
    def test_single_run(self, mock_ds, mock_runners, mock_util, mock_sim, capsys, tmp_path):
        mock_ds.return_value = FAKE_DS
        mock_runners.return_value = [
            {"name": "l-x86iavx512-8-16", "instance_type": "c7a.48xlarge", "vcpu": 8, "memory_mi": 16384, "gpu": 0}
        ]
        mock_sim.return_value = SimResult(
            nodes=[SimNode("c7a.48xlarge", 10000, 20000, 0, used_cpu_m=8000, used_mem_mi=15000)],
            deployed={"l-x86iavx512-8-16": 10},
            targets={"l-x86iavx512-8-16": 10},
            skipped_labels={},
        )
        mock_util.return_value = {
            "cpu_pct": 80.0,
            "mem_pct": 75.0,
            "gpu_pct": 0.0,
            "total_cpu_m": 10000,
            "used_cpu_m": 8000,
            "total_mem_mi": 20000,
            "used_mem_mi": 15000,
            "total_gpu": 0,
            "used_gpu": 0,
            "total_nodes": 1,
            "gpu_nodes": 0,
        }
        result = main(
            [
                "--upstream-dir",
                str(tmp_path),
                "--consumer-root",
                str(tmp_path),
                "--seed",
                "42",
            ]
        )
        assert result == 0

    @patch("simulate_cluster_cli.load_runner_defs", return_value=[])
    @patch("simulate_cluster_cli.discover_daemonsets", return_value=FAKE_DS)
    def test_no_runners_returns_1(self, mock_ds, mock_runners, capsys, tmp_path):
        result = main(
            [
                "--upstream-dir",
                str(tmp_path),
                "--consumer-root",
                str(tmp_path),
            ]
        )
        assert result == 1

    @patch("simulate_cluster_cli.run_simulation")
    @patch("simulate_cluster_cli.compute_utilization")
    @patch("simulate_cluster_cli.load_runner_defs")
    @patch("simulate_cluster_cli.discover_daemonsets")
    def test_multi_round(self, mock_ds, mock_runners, mock_util, mock_sim, capsys, tmp_path):
        mock_ds.return_value = FAKE_DS
        mock_runners.return_value = [
            {"name": "l-x86iavx512-8-16", "instance_type": "c7a.48xlarge", "vcpu": 8, "memory_mi": 16384, "gpu": 0}
        ]
        mock_sim.return_value = SimResult(
            nodes=[SimNode("c7a.48xlarge", 10000, 20000, 0, used_cpu_m=8000, used_mem_mi=15000)],
            deployed={"l-x86iavx512-8-16": 10},
            targets={"l-x86iavx512-8-16": 10},
        )
        mock_util.return_value = {
            "cpu_pct": 80.0,
            "mem_pct": 75.0,
            "gpu_pct": 0.0,
            "total_gpu": 0,
            "total_nodes": 1,
        }
        result = main(
            [
                "--upstream-dir",
                str(tmp_path),
                "--consumer-root",
                str(tmp_path),
                "--rounds",
                "3",
            ]
        )
        assert result == 0

    @patch("simulate_cluster_cli.run_simulation")
    @patch("simulate_cluster_cli.compute_utilization")
    @patch("simulate_cluster_cli.load_runner_defs")
    @patch("simulate_cluster_cli.discover_daemonsets")
    def test_env_osdc_root(self, mock_ds, mock_runners, mock_util, mock_sim, capsys, tmp_path, monkeypatch):
        """Tests the OSDC_ROOT env var path."""
        mock_ds.return_value = FAKE_DS
        mock_runners.return_value = [
            {"name": "l-x86iavx512-8-16", "instance_type": "c7a.48xlarge", "vcpu": 8, "memory_mi": 16384, "gpu": 0}
        ]
        mock_sim.return_value = SimResult(
            nodes=[SimNode("c7a.48xlarge", 10000, 20000, 0, used_cpu_m=8000, used_mem_mi=15000)],
            deployed={"l-x86iavx512-8-16": 10},
            targets={"l-x86iavx512-8-16": 10},
            skipped_labels={},
        )
        mock_util.return_value = {
            "cpu_pct": 80.0,
            "mem_pct": 75.0,
            "gpu_pct": 0.0,
            "total_cpu_m": 10000,
            "used_cpu_m": 8000,
            "total_mem_mi": 20000,
            "used_mem_mi": 15000,
            "total_gpu": 0,
            "used_gpu": 0,
            "total_nodes": 1,
            "gpu_nodes": 0,
        }
        monkeypatch.setenv("OSDC_ROOT", str(tmp_path))
        result = main(["--upstream-dir", str(tmp_path)])
        assert result == 0

    @patch("simulate_cluster_cli.run_simulation")
    @patch("simulate_cluster_cli.compute_utilization")
    @patch("simulate_cluster_cli.load_runner_defs")
    @patch("simulate_cluster_cli.discover_daemonsets")
    def test_candidate_fallback_no_clusters_yaml(
        self, mock_ds, mock_runners, mock_util, mock_sim, capsys, tmp_path, monkeypatch
    ):
        """When OSDC_ROOT unset and candidate has no clusters.yaml, falls back to upstream (lines 196-197)."""
        mock_ds.return_value = FAKE_DS
        mock_runners.return_value = [
            {"name": "r1", "instance_type": "c7a.48xlarge", "vcpu": 8, "memory_mi": 16384, "gpu": 0}
        ]
        mock_sim.return_value = SimResult(
            nodes=[SimNode("c7a.48xlarge", 10000, 20000, 0, used_cpu_m=8000, used_mem_mi=15000)],
            deployed={"r1": 10},
            targets={"r1": 10},
            skipped_labels={},
        )
        mock_util.return_value = {
            "cpu_pct": 80.0,
            "mem_pct": 75.0,
            "gpu_pct": 0.0,
            "total_cpu_m": 10000,
            "used_cpu_m": 8000,
            "total_mem_mi": 20000,
            "used_mem_mi": 15000,
            "total_gpu": 0,
            "used_gpu": 0,
            "total_nodes": 1,
            "gpu_nodes": 0,
        }
        monkeypatch.delenv("OSDC_ROOT", raising=False)
        # Use a deep tmp_path so candidate.parent.parent won't have clusters.yaml
        upstream = tmp_path / "a" / "b" / "upstream"
        upstream.mkdir(parents=True)
        result = main(["--upstream-dir", str(upstream)])
        assert result == 0

    @patch("simulate_cluster_cli.run_simulation")
    @patch("simulate_cluster_cli.compute_utilization")
    @patch("simulate_cluster_cli.load_runner_defs")
    @patch("simulate_cluster_cli.discover_daemonsets")
    def test_consumer_with_runner_modules(self, mock_ds, mock_runners, mock_util, mock_sim, capsys, tmp_path):
        """Tests consumer_root != upstream with runner modules."""
        consumer = tmp_path / "consumer"
        upstream = tmp_path / "upstream"
        consumer.mkdir()
        upstream.mkdir()
        mod = consumer / "modules" / "custom-runners" / "defs"
        mod.mkdir(parents=True)
        # Write a runner def that will be found by the iterdir scanning
        import yaml

        runner_def = {"runner": {"name": "custom-r", "instance_type": "c7a.48xlarge", "vcpu": 4, "memory": "8Gi"}}
        (mod / "custom.yaml").write_text(yaml.dump(runner_def))

        mock_ds.return_value = FAKE_DS
        mock_runners.return_value = [
            {"name": "l-x86iavx512-8-16", "instance_type": "c7a.48xlarge", "vcpu": 8, "memory_mi": 16384, "gpu": 0}
        ]
        mock_sim.return_value = SimResult(
            nodes=[SimNode("c7a.48xlarge", 10000, 20000, 0, used_cpu_m=8000, used_mem_mi=15000)],
            deployed={"l-x86iavx512-8-16": 10},
            targets={"l-x86iavx512-8-16": 10},
            skipped_labels={},
        )
        mock_util.return_value = {
            "cpu_pct": 80.0,
            "mem_pct": 75.0,
            "gpu_pct": 0.0,
            "total_cpu_m": 10000,
            "used_cpu_m": 8000,
            "total_mem_mi": 20000,
            "used_mem_mi": 15000,
            "total_gpu": 0,
            "used_gpu": 0,
            "total_nodes": 1,
            "gpu_nodes": 0,
        }
        result = main(
            [
                "--upstream-dir",
                str(upstream),
                "--consumer-root",
                str(consumer),
            ]
        )
        assert result == 0
