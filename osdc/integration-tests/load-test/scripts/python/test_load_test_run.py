"""Unit tests for load_test_run.py — load test orchestrator CLI."""

import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest
from distribution import RunnerAllocation
from load_test_monitor import LoadTestResults
from load_test_run import (
    DEFAULT_TOTAL_JOBS,
    LOAD_TEST_PR_TITLE_PREFIX,
    _prepare_load_test_pr,
    _print_distribution,
    branch_name,
    main,
    parse_args,
)


# ── branch_name ─────────────────────────────────────────────────────────


class TestBranchName:
    def test_basic(self):
        assert branch_name("arc-staging") == "osdc-load-test-arc-staging"

    def test_production(self):
        assert branch_name("arc-prod") == "osdc-load-test-arc-prod"


# ── parse_args ──────────────────────────────────────────────────────────


class TestParseArgs:
    def test_required_args(self):
        with patch(
            "sys.argv",
            [
                "load_test_run.py",
                "--cluster-id",
                "test-cluster",
                "--clusters-yaml",
                "/tmp/clusters.yaml",
                "--upstream-dir",
                "/tmp/upstream",
                "--root-dir",
                "/tmp/root",
            ],
        ):
            args = parse_args()
            assert args.cluster_id == "test-cluster"
            assert args.clusters_yaml == Path("/tmp/clusters.yaml")
            assert args.upstream_dir == Path("/tmp/upstream")
            assert args.root_dir == Path("/tmp/root")
            assert args.jobs == DEFAULT_TOTAL_JOBS
            assert args.dry_run is False
            assert args.keep_pr is False
            assert args.timeout == 120

    def test_custom_jobs(self):
        with patch(
            "sys.argv",
            [
                "load_test_run.py",
                "--cluster-id",
                "x",
                "--clusters-yaml",
                "/tmp/c.yaml",
                "--upstream-dir",
                "/tmp/u",
                "--root-dir",
                "/tmp/r",
                "--jobs",
                "50",
            ],
        ):
            args = parse_args()
            assert args.jobs == 50

    def test_dry_run_flag(self):
        with patch(
            "sys.argv",
            [
                "load_test_run.py",
                "--cluster-id",
                "x",
                "--clusters-yaml",
                "/tmp/c.yaml",
                "--upstream-dir",
                "/tmp/u",
                "--root-dir",
                "/tmp/r",
                "--dry-run",
            ],
        ):
            args = parse_args()
            assert args.dry_run is True

    def test_keep_pr_flag(self):
        with patch(
            "sys.argv",
            [
                "load_test_run.py",
                "--cluster-id",
                "x",
                "--clusters-yaml",
                "/tmp/c.yaml",
                "--upstream-dir",
                "/tmp/u",
                "--root-dir",
                "/tmp/r",
                "--keep-pr",
            ],
        ):
            args = parse_args()
            assert args.keep_pr is True

    def test_custom_timeout(self):
        with patch(
            "sys.argv",
            [
                "load_test_run.py",
                "--cluster-id",
                "x",
                "--clusters-yaml",
                "/tmp/c.yaml",
                "--upstream-dir",
                "/tmp/u",
                "--root-dir",
                "/tmp/r",
                "--timeout",
                "60",
            ],
        ):
            args = parse_args()
            assert args.timeout == 60

    def test_zero_jobs_rejected(self):
        with (
            patch(
                "sys.argv",
                [
                    "load_test_run.py",
                    "--cluster-id",
                    "x",
                    "--clusters-yaml",
                    "/tmp/c.yaml",
                    "--upstream-dir",
                    "/tmp/u",
                    "--root-dir",
                    "/tmp/r",
                    "--jobs",
                    "0",
                ],
            ),
            pytest.raises(SystemExit),
        ):
            parse_args()

    def test_negative_jobs_rejected(self):
        with (
            patch(
                "sys.argv",
                [
                    "load_test_run.py",
                    "--cluster-id",
                    "x",
                    "--clusters-yaml",
                    "/tmp/c.yaml",
                    "--upstream-dir",
                    "/tmp/u",
                    "--root-dir",
                    "/tmp/r",
                    "--jobs",
                    "-5",
                ],
            ),
            pytest.raises(SystemExit),
        ):
            parse_args()

    def test_negative_timeout_rejected(self):
        with (
            patch(
                "sys.argv",
                [
                    "load_test_run.py",
                    "--cluster-id",
                    "x",
                    "--clusters-yaml",
                    "/tmp/c.yaml",
                    "--upstream-dir",
                    "/tmp/u",
                    "--root-dir",
                    "/tmp/r",
                    "--timeout",
                    "-1",
                ],
            ),
            pytest.raises(SystemExit),
        ):
            parse_args()

    def test_missing_required_arg(self):
        with patch("sys.argv", ["load_test_run.py"]), pytest.raises(SystemExit):
            parse_args()


# ── _print_distribution ────────────────────────────────────────────────


class TestPrintDistribution:
    def _alloc(self, label, jobs, is_gpu=False, is_arm64=False, gpu_count=0):
        return RunnerAllocation(label, jobs, 1000, 0.5, is_gpu, is_arm64, gpu_count)

    def test_cpu_runner(self, capsys):
        allocs = [self._alloc("l-x86iavx512-8-16", 10)]
        _print_distribution(allocs, "test-cluster")
        out = capsys.readouterr().out
        assert "l-x86iavx512-8-16" in out
        assert "CPU" in out
        assert "Total" in out

    def test_arm_runner(self, capsys):
        allocs = [self._alloc("l-arm64g3-16-62", 5, is_arm64=True)]
        _print_distribution(allocs, "test")
        out = capsys.readouterr().out
        assert "ARM" in out

    def test_gpu_single(self, capsys):
        allocs = [self._alloc("l-x86iavx512-29-115-t4", 3, is_gpu=True, gpu_count=1)]
        _print_distribution(allocs, "test")
        out = capsys.readouterr().out
        assert "GPU" in out

    def test_gpu_multi(self, capsys):
        allocs = [self._alloc("l-x86iavx512-45-172-t4-4", 2, is_gpu=True, gpu_count=4)]
        _print_distribution(allocs, "test")
        out = capsys.readouterr().out
        assert "GPU*4" in out

    def test_total_line(self, capsys):
        allocs = [
            self._alloc("l-x86iavx512-8-16", 7),
            self._alloc("l-arm64g3-16-62", 3, is_arm64=True),
        ]
        _print_distribution(allocs, "test")
        out = capsys.readouterr().out
        assert "10" in out


# ── _prepare_load_test_pr ──────────────────────────────────────────────


class TestPrepareLoadTestPr:
    @patch("load_test_run.run_cmd")
    def test_creates_pr(self, mock_run_cmd, tmp_path):
        canary = tmp_path / "canary"
        canary.mkdir()
        workflows_dir = canary / ".github" / "workflows"
        workflows_dir.mkdir(parents=True)
        (workflows_dir / "old.yaml").write_text("old")

        # run_cmd return values in order:
        # 1. git fetch
        # 2. git checkout
        # 3. git add
        # 4. git diff --cached --quiet (returncode=1 means changes)
        # 5. git commit
        # 6. git push
        # 7. gh pr create -> returns PR URL
        diff_result = MagicMock(returncode=1)
        pr_result = MagicMock(stdout="https://github.com/pytorch/pytorch-canary/pull/42\n")
        mock_run_cmd.side_effect = [
            MagicMock(),  # git fetch
            MagicMock(),  # git checkout
            MagicMock(),  # git add
            diff_result,  # git diff
            MagicMock(),  # git commit
            MagicMock(),  # git push
            pr_result,  # gh pr create
        ]

        result = _prepare_load_test_pr(
            canary,
            tmp_path / "upstream",
            "workflow: content",
            "test-branch",
        )
        assert result == 42
        # Old workflow file should be removed
        assert not (workflows_dir / "old.yaml").exists()
        # New workflow should be written
        assert (workflows_dir / "load-test.yaml").read_text() == "workflow: content"

    @patch("load_test_run.run_cmd")
    def test_no_changes_skips_commit(self, mock_run_cmd, tmp_path):
        canary = tmp_path / "canary"
        canary.mkdir()

        # git diff --cached --quiet returns 0 (no changes)
        diff_result = MagicMock(returncode=0)
        pr_result = MagicMock(stdout="https://github.com/pytorch/pytorch-canary/pull/7\n")
        mock_run_cmd.side_effect = [
            MagicMock(),  # git fetch
            MagicMock(),  # git checkout
            MagicMock(),  # git add
            diff_result,  # git diff (no changes)
            MagicMock(),  # git push (skip commit)
            pr_result,  # gh pr create
        ]

        result = _prepare_load_test_pr(
            canary,
            tmp_path / "upstream",
            "content",
            "branch",
        )
        assert result == 7

    @patch("load_test_run.run_cmd")
    def test_creates_workflows_dir(self, mock_run_cmd, tmp_path):
        canary = tmp_path / "canary"
        canary.mkdir()
        # No .github/workflows dir exists yet

        diff_result = MagicMock(returncode=0)
        pr_result = MagicMock(stdout="https://github.com/pytorch/pytorch-canary/pull/1\n")
        mock_run_cmd.side_effect = [
            MagicMock(),  # git fetch
            MagicMock(),  # git checkout
            MagicMock(),  # git add
            diff_result,  # git diff
            MagicMock(),  # git push
            pr_result,  # gh pr create
        ]

        result = _prepare_load_test_pr(
            canary,
            tmp_path / "upstream",
            "content",
            "branch",
        )
        assert result == 1
        assert (canary / ".github" / "workflows" / "load-test.yaml").exists()


# ── main ────────────────────────────────────────────────────────────────


class TestMain:
    def _base_argv(self, **overrides):
        argv = [
            "load_test_run.py",
            "--cluster-id",
            overrides.get("cluster_id", "test-cluster"),
            "--clusters-yaml",
            overrides.get("clusters_yaml", "/tmp/c.yaml"),
            "--upstream-dir",
            overrides.get("upstream_dir", "/tmp/upstream"),
            "--root-dir",
            overrides.get("root_dir", "/tmp/root"),
        ]
        if overrides.get("dry_run"):
            argv.append("--dry-run")
        if overrides.get("keep_pr"):
            argv.append("--keep-pr")
        if "jobs" in overrides:
            argv.extend(["--jobs", str(overrides["jobs"])])
        if "timeout" in overrides:
            argv.extend(["--timeout", str(overrides["timeout"])])
        return argv

    def _alloc(self, label="l-x86iavx512-8-16", jobs=5):
        return RunnerAllocation(label, jobs, 1000, 0.5, False, False, 0)

    @patch("load_test_run.run_cmd")
    @patch("load_test_run.print_load_test_report")
    @patch("load_test_run.wait_for_load_test")
    @patch("load_test_run._prepare_load_test_pr")
    @patch("load_test_run.ensure_canary_repo")
    @patch("load_test_run.cleanup_stale_prs")
    @patch("load_test_run.generate_workflow")
    @patch("load_test_run.compute_distribution")
    @patch("load_test_run.get_available_runners")
    @patch("load_test_run.load_cluster_config")
    def test_full_pass(
        self,
        mock_load_cfg,
        mock_get_runners,
        mock_compute,
        mock_gen_wf,
        mock_cleanup,
        mock_ensure,
        mock_prepare_pr,
        mock_wait,
        mock_report,
        mock_run_cmd,
    ):
        mock_load_cfg.return_value = {
            "cluster": {"cluster_name": "test", "arc-runners": {"runner_name_prefix": "mt-"}},
            "defaults": {},
        }
        mock_get_runners.return_value = {"l-x86iavx512-8-16"}
        mock_compute.return_value = [self._alloc()]
        mock_gen_wf.return_value = "workflow: yaml"
        mock_ensure.return_value = Path("/tmp/canary")
        mock_prepare_pr.return_value = 42
        mock_wait.return_value = LoadTestResults(5, 5, False, 60.0, [], [1])
        mock_report.return_value = True

        with patch("sys.argv", self._base_argv(jobs=5)):
            with pytest.raises(SystemExit) as exc:
                main()
            assert exc.value.code == 0

        mock_cleanup.assert_called_once()
        mock_report.assert_called_once()
        # PR should be closed (no --keep-pr)
        mock_run_cmd.assert_called_once()
        close_call = mock_run_cmd.call_args
        assert "close" in close_call[0][0]

    @patch("load_test_run.generate_workflow")
    @patch("load_test_run.compute_distribution")
    @patch("load_test_run.get_available_runners")
    @patch("load_test_run.load_cluster_config")
    def test_dry_run(
        self,
        mock_load_cfg,
        mock_get_runners,
        mock_compute,
        mock_gen_wf,
        capsys,
    ):
        mock_load_cfg.return_value = {
            "cluster": {"cluster_name": "t"},
            "defaults": {},
        }
        mock_get_runners.return_value = {"l-x86iavx512-8-16"}
        mock_compute.return_value = [self._alloc()]
        mock_gen_wf.return_value = "workflow: yaml"

        with patch("sys.argv", self._base_argv(dry_run=True, jobs=5)):
            with pytest.raises(SystemExit) as exc:
                main()
            assert exc.value.code == 0

        out = capsys.readouterr().out
        assert "workflow: yaml" in out

    @patch("load_test_run.compute_distribution")
    @patch("load_test_run.get_available_runners")
    @patch("load_test_run.load_cluster_config")
    def test_no_allocations_exits_1(
        self,
        mock_load_cfg,
        mock_get_runners,
        mock_compute,
    ):
        mock_load_cfg.return_value = {
            "cluster": {"cluster_name": "t"},
            "defaults": {},
        }
        mock_get_runners.return_value = set()
        mock_compute.return_value = []

        with patch("sys.argv", self._base_argv(jobs=5)):
            with pytest.raises(SystemExit) as exc:
                main()
            assert exc.value.code == 1

    @patch("load_test_run.run_cmd")
    @patch("load_test_run.print_load_test_report")
    @patch("load_test_run.wait_for_load_test")
    @patch("load_test_run._prepare_load_test_pr")
    @patch("load_test_run.ensure_canary_repo")
    @patch("load_test_run.cleanup_stale_prs")
    @patch("load_test_run.generate_workflow")
    @patch("load_test_run.compute_distribution")
    @patch("load_test_run.get_available_runners")
    @patch("load_test_run.load_cluster_config")
    def test_pr_creation_fails_exits_1(
        self,
        mock_load_cfg,
        mock_get_runners,
        mock_compute,
        mock_gen_wf,
        mock_cleanup,
        mock_ensure,
        mock_prepare_pr,
        mock_wait,
        mock_report,
        mock_run_cmd,
    ):
        mock_load_cfg.return_value = {
            "cluster": {"cluster_name": "t"},
            "defaults": {},
        }
        mock_get_runners.return_value = {"l-x86iavx512-8-16"}
        mock_compute.return_value = [self._alloc()]
        mock_gen_wf.return_value = "wf"
        mock_ensure.return_value = Path("/tmp/canary")
        mock_prepare_pr.return_value = None  # PR creation failed

        with patch("sys.argv", self._base_argv(jobs=5)):
            with pytest.raises(SystemExit) as exc:
                main()
            assert exc.value.code == 1

    @patch("load_test_run.run_cmd")
    @patch("load_test_run.print_load_test_report")
    @patch("load_test_run.wait_for_load_test")
    @patch("load_test_run._prepare_load_test_pr")
    @patch("load_test_run.ensure_canary_repo")
    @patch("load_test_run.cleanup_stale_prs")
    @patch("load_test_run.generate_workflow")
    @patch("load_test_run.compute_distribution")
    @patch("load_test_run.get_available_runners")
    @patch("load_test_run.load_cluster_config")
    def test_keep_pr_skips_close(
        self,
        mock_load_cfg,
        mock_get_runners,
        mock_compute,
        mock_gen_wf,
        mock_cleanup,
        mock_ensure,
        mock_prepare_pr,
        mock_wait,
        mock_report,
        mock_run_cmd,
    ):
        mock_load_cfg.return_value = {
            "cluster": {"cluster_name": "t"},
            "defaults": {},
        }
        mock_get_runners.return_value = {"l-x86iavx512-8-16"}
        mock_compute.return_value = [self._alloc()]
        mock_gen_wf.return_value = "wf"
        mock_ensure.return_value = Path("/tmp/canary")
        mock_prepare_pr.return_value = 99
        mock_wait.return_value = LoadTestResults(5, 5, False, 60.0, [], [1])
        mock_report.return_value = True

        with patch("sys.argv", self._base_argv(keep_pr=True, jobs=5)):
            with pytest.raises(SystemExit) as exc:
                main()
            assert exc.value.code == 0

        # run_cmd should NOT be called (no PR close)
        mock_run_cmd.assert_not_called()

    @patch("load_test_run.run_cmd")
    @patch("load_test_run.print_load_test_report")
    @patch("load_test_run.wait_for_load_test")
    @patch("load_test_run._prepare_load_test_pr")
    @patch("load_test_run.ensure_canary_repo")
    @patch("load_test_run.cleanup_stale_prs")
    @patch("load_test_run.generate_workflow")
    @patch("load_test_run.compute_distribution")
    @patch("load_test_run.get_available_runners")
    @patch("load_test_run.load_cluster_config")
    def test_failed_report_exits_1(
        self,
        mock_load_cfg,
        mock_get_runners,
        mock_compute,
        mock_gen_wf,
        mock_cleanup,
        mock_ensure,
        mock_prepare_pr,
        mock_wait,
        mock_report,
        mock_run_cmd,
    ):
        mock_load_cfg.return_value = {
            "cluster": {"cluster_name": "t"},
            "defaults": {},
        }
        mock_get_runners.return_value = {"l-x86iavx512-8-16"}
        mock_compute.return_value = [self._alloc()]
        mock_gen_wf.return_value = "wf"
        mock_ensure.return_value = Path("/tmp/canary")
        mock_prepare_pr.return_value = 10
        mock_wait.return_value = LoadTestResults(5, 3, False, 60.0, [], [1])
        mock_report.return_value = False  # test failed

        with patch("sys.argv", self._base_argv(jobs=5)):
            with pytest.raises(SystemExit) as exc:
                main()
            assert exc.value.code == 1

    @patch("load_test_run.run_cmd")
    @patch("load_test_run.print_load_test_report")
    @patch("load_test_run.wait_for_load_test")
    @patch("load_test_run._prepare_load_test_pr")
    @patch("load_test_run.ensure_canary_repo")
    @patch("load_test_run.cleanup_stale_prs")
    @patch("load_test_run.generate_workflow")
    @patch("load_test_run.compute_distribution")
    @patch("load_test_run.get_available_runners")
    @patch("load_test_run.load_cluster_config")
    def test_pr_closed_on_exception(
        self,
        mock_load_cfg,
        mock_get_runners,
        mock_compute,
        mock_gen_wf,
        mock_cleanup,
        mock_ensure,
        mock_prepare_pr,
        mock_wait,
        mock_report,
        mock_run_cmd,
    ):
        """PR is closed even when wait_for_load_test raises an exception."""
        mock_load_cfg.return_value = {
            "cluster": {"cluster_name": "t"},
            "defaults": {},
        }
        mock_get_runners.return_value = {"l-x86iavx512-8-16"}
        mock_compute.return_value = [self._alloc()]
        mock_gen_wf.return_value = "wf"
        mock_ensure.return_value = Path("/tmp/canary")
        mock_prepare_pr.return_value = 55
        mock_wait.side_effect = RuntimeError("boom")

        with patch("sys.argv", self._base_argv(jobs=5)):
            with pytest.raises(RuntimeError, match="boom"):
                main()

        # PR should still be closed via finally block
        mock_run_cmd.assert_called_once()
        close_call = mock_run_cmd.call_args
        assert "close" in close_call[0][0]
        assert "55" in close_call[0][0]
