"""Tests for analyze_node_utilization module."""

from pathlib import Path
from unittest.mock import patch

import yaml
from analyze_node_utilization import (
    _print_combo,
    compute_allocatable,
    compute_daemonset_overhead,
    compute_node_slack,
    find_maximal_combos,
    find_valid_combos,
    format_mem,
    kubelet_reserved,
    load_nodepool_defs,
    load_runner_defs,
    main,
    parse_memory,
    per_runner_total,
    print_node_analysis,
)
from daemonset_overhead import DaemonSetOverhead

FAKE_DS = [
    DaemonSetOverhead("kube-proxy", 50, 80, False, "test"),
    DaemonSetOverhead("gpu-plugin", 100, 256, True, "test"),
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
# compute_daemonset_overhead
# ---------------------------------------------------------------------------


class TestComputeDaemonsetOverhead:
    def test_cpu_only_node(self):
        cpu, mem = compute_daemonset_overhead(FAKE_DS, is_gpu=False)
        assert cpu == 50  # only kube-proxy
        assert mem == 80

    def test_gpu_node(self):
        cpu, mem = compute_daemonset_overhead(FAKE_DS, is_gpu=True)
        assert cpu == 150  # kube-proxy + gpu-plugin
        assert mem == 336

    def test_empty_daemonsets(self):
        cpu, mem = compute_daemonset_overhead([], is_gpu=True)
        assert cpu == 0
        assert mem == 0


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
# per_runner_total
# ---------------------------------------------------------------------------


class TestPerRunnerTotal:
    def test_basic(self):
        runner = {"vcpu": 8, "memory_mi": 16384, "gpu": 0}
        cpu, mem, gpu = per_runner_total(runner)
        # 8*1000 + 750 (sidecar) + 320 (hooks) = 9070
        assert cpu == 9070
        # 16384 + 512 (sidecar) + 522 (hooks) = 17418
        assert mem == 17418
        assert gpu == 0

    def test_with_gpu(self):
        runner = {"vcpu": 16, "memory_mi": 32768, "gpu": 4}
        _, _, gpu = per_runner_total(runner)
        assert gpu == 4


# ---------------------------------------------------------------------------
# load_runner_defs
# ---------------------------------------------------------------------------


class TestLoadRunnerDefs:
    def test_loads_runner_yaml(self, tmp_path):
        runner_def = {
            "runner": {
                "name": "test-runner",
                "instance_type": "c7a.48xlarge",
                "vcpu": 8,
                "memory": "16Gi",
                "gpu": 0,
            }
        }
        (tmp_path / "runner.yaml").write_text(yaml.dump(runner_def))
        runners = load_runner_defs([tmp_path])
        assert len(runners) == 1
        assert runners[0]["name"] == "test-runner"
        assert runners[0]["vcpu"] == 8
        assert runners[0]["memory_mi"] == 16384

    def test_skips_non_runner_yaml(self, tmp_path):
        (tmp_path / "config.yaml").write_text(yaml.dump({"settings": {"key": "value"}}))
        runners = load_runner_defs([tmp_path])
        assert len(runners) == 0

    def test_empty_yaml(self, tmp_path):
        (tmp_path / "empty.yaml").write_text("")
        runners = load_runner_defs([tmp_path])
        assert len(runners) == 0

    def test_nonexistent_dir(self):
        runners = load_runner_defs([Path("/nonexistent")])
        assert runners == []

    def test_dedup_same_name(self, tmp_path):
        """Last directory wins for same runner name."""
        dir1 = tmp_path / "d1"
        dir2 = tmp_path / "d2"
        dir1.mkdir()
        dir2.mkdir()
        r1 = {"runner": {"name": "r1", "instance_type": "c7a.48xlarge", "vcpu": 8, "memory": "16Gi"}}
        r2 = {"runner": {"name": "r1", "instance_type": "c7a.48xlarge", "vcpu": 16, "memory": "32Gi"}}
        (dir1 / "r1.yaml").write_text(yaml.dump(r1))
        (dir2 / "r1.yaml").write_text(yaml.dump(r2))
        runners = load_runner_defs([dir1, dir2])
        assert len(runners) == 1
        assert runners[0]["vcpu"] == 16

    def test_dedup_same_resolved_path(self, tmp_path):
        """Duplicate resolved directories are skipped."""
        runner_def = {
            "runner": {
                "name": "test-runner",
                "instance_type": "c7a.48xlarge",
                "vcpu": 8,
                "memory": "16Gi",
            }
        }
        (tmp_path / "r.yaml").write_text(yaml.dump(runner_def))
        runners = load_runner_defs([tmp_path, tmp_path])
        assert len(runners) == 1


# ---------------------------------------------------------------------------
# load_nodepool_defs
# ---------------------------------------------------------------------------


class TestLoadNodepoolDefs:
    def test_loads_nodepool(self, tmp_path):
        np_def = {
            "nodepool": {
                "name": "cpu-pool",
                "instance_type": "c7a.48xlarge",
                "gpu": False,
            }
        }
        (tmp_path / "np.yaml").write_text(yaml.dump(np_def))
        nodepools = load_nodepool_defs([tmp_path])
        assert "c7a.48xlarge" in nodepools
        assert nodepools["c7a.48xlarge"]["name"] == "cpu-pool"

    def test_skips_non_nodepool(self, tmp_path):
        (tmp_path / "other.yaml").write_text(yaml.dump({"runner": {"name": "r"}}))
        nodepools = load_nodepool_defs([tmp_path])
        assert nodepools == {}

    def test_nonexistent_dir(self):
        nodepools = load_nodepool_defs([Path("/nonexistent")])
        assert nodepools == {}

    def test_dedup_resolved_path(self, tmp_path):
        np_def = {
            "nodepool": {
                "name": "pool",
                "instance_type": "c7a.48xlarge",
            }
        }
        (tmp_path / "np.yaml").write_text(yaml.dump(np_def))
        nodepools = load_nodepool_defs([tmp_path, tmp_path])
        assert len(nodepools) == 1


# ---------------------------------------------------------------------------
# compute_allocatable
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
        alloc = compute_allocatable("z99.nonexistent", [])
        assert alloc is None

    def test_gpu_instance(self):
        ds = [DaemonSetOverhead("gpu-ds", 100, 200, True, "test")]
        alloc = compute_allocatable("g5.8xlarge", ds)
        assert alloc is not None
        assert alloc["is_gpu"] is True
        assert alloc["allocatable_gpu"] == 1


# ---------------------------------------------------------------------------
# find_valid_combos
# ---------------------------------------------------------------------------


class TestFindValidCombos:
    def test_single_runner_fits(self):
        alloc = {
            "allocatable_cpu_m": 10000,
            "allocatable_mem_mi": 20000,
            "allocatable_gpu": 0,
        }
        runners = [{"name": "r1", "vcpu": 2, "memory_mi": 4096, "gpu": 0}]
        combos = find_valid_combos(runners, alloc, max_pods=5)
        assert len(combos) > 0
        assert all(c["cpu_used_m"] <= 10000 for c in combos)

    def test_no_fit(self):
        alloc = {
            "allocatable_cpu_m": 100,
            "allocatable_mem_mi": 100,
            "allocatable_gpu": 0,
        }
        runners = [{"name": "r1", "vcpu": 8, "memory_mi": 16384, "gpu": 0}]
        combos = find_valid_combos(runners, alloc, max_pods=5)
        assert len(combos) == 0


# ---------------------------------------------------------------------------
# find_maximal_combos
# ---------------------------------------------------------------------------


class TestFindMaximalCombos:
    def test_filters_non_maximal(self):
        alloc = {
            "allocatable_cpu_m": 10000,
            "allocatable_mem_mi": 20000,
            "allocatable_gpu": 0,
        }
        runners = [{"name": "r1", "vcpu": 2, "memory_mi": 4096, "gpu": 0}]
        combos = find_valid_combos(runners, alloc, max_pods=5)
        maximal = find_maximal_combos(combos, alloc, runners)
        # Only the largest count should remain
        for combo in maximal:
            # No more runners can fit
            c, m, _g = per_runner_total(runners[0])
            remaining_cpu = alloc["allocatable_cpu_m"] - combo["cpu_used_m"]
            remaining_mem = alloc["allocatable_mem_mi"] - combo["mem_used_mi"]
            assert remaining_cpu < c or remaining_mem < m


# ---------------------------------------------------------------------------
# compute_node_slack
# ---------------------------------------------------------------------------


class TestComputeNodeSlack:
    def test_homogeneous_only(self):
        alloc = {
            "allocatable_cpu_m": 10000,
            "allocatable_mem_mi": 20000,
            "allocatable_gpu": 0,
        }
        runners = [{"name": "r1", "vcpu": 2, "memory_mi": 4096, "gpu": 0}]
        slack = compute_node_slack(alloc, runners, homogeneous_only=True)
        assert slack is not None
        assert "min_cpu_m" in slack

    def test_many_runners_forces_homogeneous(self):
        """More than 8 runner types forces homogeneous-only path."""
        alloc = {
            "allocatable_cpu_m": 100000,
            "allocatable_mem_mi": 200000,
            "allocatable_gpu": 0,
        }
        runners = [{"name": f"r{i}", "vcpu": 2, "memory_mi": 4096, "gpu": 0} for i in range(10)]
        slack = compute_node_slack(alloc, runners)
        assert slack is not None

    def test_no_valid_combos(self):
        alloc = {
            "allocatable_cpu_m": 100,
            "allocatable_mem_mi": 100,
            "allocatable_gpu": 0,
        }
        runners = [{"name": "r1", "vcpu": 8, "memory_mi": 16384, "gpu": 0}]
        slack = compute_node_slack(alloc, runners, homogeneous_only=True)
        assert slack is None

    def test_mixed_combos_path(self):
        """Few runners triggers the full enumeration path."""
        alloc = {
            "allocatable_cpu_m": 20000,
            "allocatable_mem_mi": 40000,
            "allocatable_gpu": 0,
        }
        runners = [
            {"name": "r1", "vcpu": 2, "memory_mi": 4096, "gpu": 0},
            {"name": "r2", "vcpu": 4, "memory_mi": 8192, "gpu": 0},
        ]
        slack = compute_node_slack(alloc, runners, homogeneous_only=False)
        assert slack is not None

    def test_mixed_no_maximal_returns_none(self):
        """Mixed path returns None when no maximal combos found (line 329)."""
        alloc = {
            "allocatable_cpu_m": 100,
            "allocatable_mem_mi": 100,
            "allocatable_gpu": 0,
        }
        # Runners too big to fit; find_valid_combos returns empty -> maximal empty
        runners = [{"name": "r1", "vcpu": 8, "memory_mi": 16384, "gpu": 0}]
        slack = compute_node_slack(alloc, runners, homogeneous_only=False)
        assert slack is None


# ---------------------------------------------------------------------------
# print_node_analysis (smoke test for output, not assertions on content)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _print_combo
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
        alloc = {"allocatable_gpu": 0}
        _print_combo(combo, alloc, 90.0, 1)
        output = capsys.readouterr().out
        assert "r1" in output

    def test_prints_combo_with_gpu(self, capsys):
        combo = {
            "runners": ["r1"],
            "cpu_util": 95.0,
            "mem_util": 90.0,
            "gpu_util": 100.0,
            "cpu_waste_m": 500,
            "mem_waste_mi": 2000,
        }
        alloc = {"allocatable_gpu": 4}
        _print_combo(combo, alloc, 90.0, 1)
        output = capsys.readouterr().out
        assert "GPU" in output

    def test_color_green_above_threshold(self, capsys):
        combo = {
            "runners": ["r1"],
            "cpu_util": 95.0,
            "mem_util": 95.0,
            "gpu_util": 0.0,
            "cpu_waste_m": 500,
            "mem_waste_mi": 1000,
        }
        alloc = {"allocatable_gpu": 0}
        _print_combo(combo, alloc, 90.0, 1)
        # Just ensure no crash for above-threshold


# ---------------------------------------------------------------------------
# print_node_analysis (smoke test for output, not assertions on content)
# ---------------------------------------------------------------------------


class TestPrintNodeAnalysis:
    def test_runs_without_error(self, capsys):
        ds = [DaemonSetOverhead("ds1", 100, 200, False, "test")]
        alloc = compute_allocatable("c7a.48xlarge", ds)
        runners = [
            {"name": "r1", "vcpu": 8, "memory_mi": 16384, "gpu": 0},
            {"name": "r2", "vcpu": 16, "memory_mi": 32768, "gpu": 0},
        ]
        # Just verify it does not raise
        print_node_analysis("c7a.48xlarge", alloc, runners, 90.0)
        output = capsys.readouterr().out
        assert "c7a.48xlarge" in output

    def test_gpu_node(self, capsys):
        ds = [DaemonSetOverhead("gpu-ds", 100, 256, True, "test")]
        alloc = compute_allocatable("g5.8xlarge", ds)
        runners = [
            {"name": "r1", "vcpu": 8, "memory_mi": 16384, "gpu": 1},
        ]
        print_node_analysis("g5.8xlarge", alloc, runners, 90.0)
        output = capsys.readouterr().out
        assert "GPU" in output

    def test_too_many_runners_skips_mixed(self, capsys):
        """More than 8 runners prints skip message."""
        ds = [DaemonSetOverhead("ds1", 50, 100, False, "test")]
        alloc = compute_allocatable("c7a.48xlarge", ds)
        runners = [{"name": f"r{i}", "vcpu": 2, "memory_mi": 4096, "gpu": 0} for i in range(10)]
        print_node_analysis("c7a.48xlarge", alloc, runners, 90.0)
        output = capsys.readouterr().out
        assert "too many runner types" in output

    def test_no_maximal_combos(self, capsys):
        """When no maximal combos found, prints skip message (lines 426-427)."""
        ds = [DaemonSetOverhead("ds1", 50, 100, False, "test")]
        alloc = compute_allocatable("c7a.48xlarge", ds)
        # Runners too big to fit at all -> no valid combos -> no maximal combos
        runners = [
            {"name": "huge", "vcpu": 999, "memory_mi": 999999, "gpu": 0},
        ]
        print_node_analysis("c7a.48xlarge", alloc, runners, 90.0)
        output = capsys.readouterr().out
        assert "no valid combos found" in output


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


class TestMain:
    def test_show_daemonsets(self, capsys):
        """--show-daemonsets flag prints daemonsets and exits."""
        # main() resolves __file__ to find upstream_dir, so we run against
        # the real project tree. --show-daemonsets prints and returns 0.
        result = main(["--show-daemonsets"])
        assert result == 0
        output = capsys.readouterr().out
        assert "Discovered DaemonSets" in output

    def test_runs_full_analysis(self, capsys):
        """Full analysis against real project defs (smoke test)."""
        # This exercises the main() code path against the real tree.
        # It returns 0 (all good) or 1 (issues found); either is fine.
        result = main(["--threshold", "99"])
        assert result in (0, 1)
        output = capsys.readouterr().out
        assert "Node Utilization Analysis" in output

    @patch("analyze_node_utilization.load_runner_defs", return_value=[])
    def test_no_runners_returns_1(self, mock_load, capsys):
        """main() returns 1 when no runner definitions found (lines 559-560)."""
        result = main(["--threshold", "90"])
        assert result == 1
        output = capsys.readouterr().out
        assert "No runner definitions found" in output

    @patch("analyze_node_utilization.load_runner_defs")
    def test_unknown_instance_type_warning(self, mock_load, capsys):
        """main() warns about unknown instance types (line 570) and skips them (line 577)."""
        mock_load.return_value = [
            {"name": "r1", "instance_type": "z99.fake", "vcpu": 4, "memory_mi": 8192, "gpu": 0},
        ]
        result = main(["--threshold", "90"])
        assert result in (0, 1)
        output = capsys.readouterr().out
        assert "z99.fake" in output

    @patch("analyze_node_utilization.load_runner_defs")
    def test_all_good_message(self, mock_load, capsys):
        """When all runners have good utilization, prints all-good message (line 607)."""
        # Use controlled runner defs that pack well on their instance type
        mock_load.return_value = [
            {"name": "r1", "instance_type": "c7a.xlarge", "vcpu": 2, "memory_mi": 4096, "gpu": 0},
        ]
        result = main(["--threshold", "1"])
        assert result == 0
        output = capsys.readouterr().out
        assert "All runner types achieve" in output

    def test_consumer_runner_defs_discovered(self, tmp_path, monkeypatch, capsys):
        """main() discovers runner defs from consumer modules/ when OSDC_ROOT is set."""
        # Create a fake consumer root with a module containing runner defs
        consumer = tmp_path / "consumer"
        defs = consumer / "modules" / "custom-runners" / "defs"
        defs.mkdir(parents=True)
        (defs / "big-runner.yaml").write_text(
            yaml.dump(
                {
                    "runner": {
                        "name": "consumer-big",
                        "instance_type": "c7a.48xlarge",
                        "vcpu": 96,
                        "memory": "192Gi",
                        "gpu": 0,
                    }
                }
            )
        )
        monkeypatch.setenv("OSDC_ROOT", str(consumer))
        result = main(["--threshold", "1"])
        assert result in (0, 1)
        output = capsys.readouterr().out
        assert str(defs) in output

    def test_consumer_nodepool_defs_discovered(self, tmp_path, monkeypatch, capsys):
        """main() discovers nodepool defs from consumer modules/ when OSDC_ROOT is set."""
        consumer = tmp_path / "consumer"
        defs = consumer / "modules" / "custom-nodepools" / "defs"
        defs.mkdir(parents=True)
        (defs / "big-pool.yaml").write_text(
            yaml.dump(
                {
                    "nodepool": {
                        "name": "consumer-pool",
                        "instance_type": "c7a.48xlarge",
                    }
                }
            )
        )
        monkeypatch.setenv("OSDC_ROOT", str(consumer))
        result = main(["--threshold", "1"])
        assert result in (0, 1)
        output = capsys.readouterr().out
        assert str(defs) in output

    def test_consumer_module_without_defs_ignored(self, tmp_path, monkeypatch, capsys):
        """Consumer modules without a defs/ directory are silently skipped."""
        consumer = tmp_path / "consumer"
        (consumer / "modules" / "no-defs-module").mkdir(parents=True)
        monkeypatch.setenv("OSDC_ROOT", str(consumer))
        result = main(["--threshold", "1"])
        assert result in (0, 1)

    def test_consumer_module_non_runner_yaml_ignored(self, tmp_path, monkeypatch, capsys):
        """Consumer module defs with non-runner/nodepool YAMLs are not added."""
        consumer = tmp_path / "consumer"
        defs = consumer / "modules" / "other-module" / "defs"
        defs.mkdir(parents=True)
        (defs / "config.yaml").write_text(yaml.dump({"something_else": True}))
        monkeypatch.setenv("OSDC_ROOT", str(consumer))
        result = main(["--threshold", "1"])
        assert result in (0, 1)
        output = capsys.readouterr().out
        assert str(defs) not in output
