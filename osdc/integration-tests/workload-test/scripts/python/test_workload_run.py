"""Unit tests for workload_run.py."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest
from workload_run import (
    DEFAULT_POLL_INTERVAL,
    DEFAULT_TIMEOUT_MINUTES,
    branch_name,
    main,
    parse_args,
)


# ── branch_name ─────────────────────────────────────────────────────────


class TestBranchName:
    def test_basic(self):
        assert branch_name("arc-staging") == "osdc-workload-test-arc-staging"

    def test_production(self):
        assert branch_name("arc-cbr-production") == "osdc-workload-test-arc-cbr-production"

    def test_short_id(self):
        assert branch_name("x") == "osdc-workload-test-x"


# ── parse_args ──────────────────────────────────────────────────────────


class TestParseArgs:
    def test_required_args(self):
        with patch(
            "sys.argv",
            [
                "workload_run.py",
                "--cluster-id",
                "test-cluster",
                "--clusters-yaml",
                "/tmp/clusters.yaml",
                "--upstream-dir",
                "/tmp/upstream",
            ],
        ):
            args = parse_args()
            assert args.cluster_id == "test-cluster"
            assert args.clusters_yaml == Path("/tmp/clusters.yaml")
            assert args.upstream_dir == Path("/tmp/upstream")
            assert args.dry_run is False
            assert args.keep_pr is False
            assert args.timeout == DEFAULT_TIMEOUT_MINUTES

    def test_dry_run_flag(self):
        with patch(
            "sys.argv",
            [
                "workload_run.py",
                "--cluster-id",
                "x",
                "--clusters-yaml",
                "/tmp/c.yaml",
                "--upstream-dir",
                "/tmp/u",
                "--dry-run",
            ],
        ):
            args = parse_args()
            assert args.dry_run is True

    def test_keep_pr_flag(self):
        with patch(
            "sys.argv",
            [
                "workload_run.py",
                "--cluster-id",
                "x",
                "--clusters-yaml",
                "/tmp/c.yaml",
                "--upstream-dir",
                "/tmp/u",
                "--keep-pr",
            ],
        ):
            args = parse_args()
            assert args.keep_pr is True

    def test_custom_timeout(self):
        with patch(
            "sys.argv",
            [
                "workload_run.py",
                "--cluster-id",
                "x",
                "--clusters-yaml",
                "/tmp/c.yaml",
                "--upstream-dir",
                "/tmp/u",
                "--timeout",
                "120",
            ],
        ):
            args = parse_args()
            assert args.timeout == 120

    def test_all_flags(self):
        with patch(
            "sys.argv",
            [
                "workload_run.py",
                "--cluster-id",
                "prod",
                "--clusters-yaml",
                "/c.yaml",
                "--upstream-dir",
                "/u",
                "--dry-run",
                "--keep-pr",
                "--timeout",
                "90",
            ],
        ):
            args = parse_args()
            assert args.dry_run is True
            assert args.keep_pr is True
            assert args.timeout == 90

    def test_zero_timeout_rejected(self):
        with (
            patch(
                "sys.argv",
                [
                    "workload_run.py",
                    "--cluster-id",
                    "x",
                    "--clusters-yaml",
                    "/tmp/c.yaml",
                    "--upstream-dir",
                    "/tmp/u",
                    "--timeout",
                    "0",
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
                    "workload_run.py",
                    "--cluster-id",
                    "x",
                    "--clusters-yaml",
                    "/tmp/c.yaml",
                    "--upstream-dir",
                    "/tmp/u",
                    "--timeout",
                    "-5",
                ],
            ),
            pytest.raises(SystemExit),
        ):
            parse_args()

    def test_missing_required_arg(self):
        with patch("sys.argv", ["workload_run.py"]), pytest.raises(SystemExit):
            parse_args()

    def test_missing_cluster_id(self):
        with (
            patch(
                "sys.argv",
                [
                    "workload_run.py",
                    "--clusters-yaml",
                    "/tmp/c.yaml",
                    "--upstream-dir",
                    "/tmp/u",
                ],
            ),
            pytest.raises(SystemExit),
        ):
            parse_args()

    def test_missing_clusters_yaml(self):
        with (
            patch(
                "sys.argv",
                [
                    "workload_run.py",
                    "--cluster-id",
                    "x",
                    "--upstream-dir",
                    "/tmp/u",
                ],
            ),
            pytest.raises(SystemExit),
        ):
            parse_args()

    def test_missing_upstream_dir(self):
        with (
            patch(
                "sys.argv",
                [
                    "workload_run.py",
                    "--cluster-id",
                    "x",
                    "--clusters-yaml",
                    "/tmp/c.yaml",
                ],
            ),
            pytest.raises(SystemExit),
        ):
            parse_args()

    def test_clusters_yaml_is_path(self):
        with patch(
            "sys.argv",
            [
                "workload_run.py",
                "--cluster-id",
                "x",
                "--clusters-yaml",
                "/some/path/clusters.yaml",
                "--upstream-dir",
                "/tmp/u",
            ],
        ):
            args = parse_args()
            assert isinstance(args.clusters_yaml, Path)

    def test_upstream_dir_is_path(self):
        with patch(
            "sys.argv",
            [
                "workload_run.py",
                "--cluster-id",
                "x",
                "--clusters-yaml",
                "/tmp/c.yaml",
                "--upstream-dir",
                "/some/path/upstream",
            ],
        ):
            args = parse_args()
            assert isinstance(args.upstream_dir, Path)


# ── main ────────────────────────────────────────────────────────────────


class TestMain:
    """Test main() orchestration logic.

    All phase functions are mocked at the workload_run module level to
    prevent any real git/network operations.
    """

    def _base_argv(self, **overrides):
        """Build a sys.argv list with required arguments."""
        return [
            "workload_run.py",
            "--cluster-id",
            overrides.get("cluster_id", "test-cluster"),
            "--clusters-yaml",
            overrides.get("clusters_yaml", "/tmp/c.yaml"),
            "--upstream-dir",
            overrides.get("upstream_dir", "/tmp/upstream"),
        ] + (
            ["--dry-run"] if overrides.get("dry_run") else []
        ) + (
            ["--keep-pr"] if overrides.get("keep_pr") else []
        ) + (
            ["--timeout", str(overrides["timeout"])] if "timeout" in overrides else []
        )

    @patch("workload_run.resolve")
    @patch("workload_run.load_cluster_config")
    def test_cluster_without_arc_runners_exits_1(
        self,
        mock_load_cfg,
        mock_resolve,
    ):
        """Cluster without arc-runners module should exit 1 immediately."""
        mock_load_cfg.return_value = {"cluster": {"cluster_name": "t", "modules": ["re"]}, "defaults": {}}
        mock_resolve.side_effect = lambda cfg, key, *args: "re-cluster" if key == "cluster_name" else (args[0] if args else None)

        with patch("sys.argv", self._base_argv(cluster_id="re-prod")):
            with pytest.raises(SystemExit) as exc:
                main()
            assert exc.value.code == 1

    @patch("workload_run.close_pr")
    @patch("workload_run.print_workload_report")
    @patch("workload_run.monitor_workflows")
    @patch("workload_run.create_workload_pr")
    @patch("workload_run.instrument_workflows")
    @patch("workload_run.mirror_content")
    @patch("workload_run.ensure_pytorch_repo")
    @patch("workload_run.ensure_canary_repo")
    @patch("workload_run.cleanup_stale_prs")
    @patch("workload_run.run_cmd")
    @patch("workload_run.resolve")
    @patch("workload_run.load_cluster_config")
    def test_happy_path_all_pass(
        self,
        mock_load_cfg,
        mock_resolve,
        mock_run_cmd,
        mock_cleanup,
        mock_ensure_canary,
        mock_ensure_pytorch,
        mock_mirror,
        mock_instrument,
        mock_create_pr,
        mock_monitor,
        mock_report,
        mock_close_pr,
    ):
        """All phases execute in order, report passes, exit 0."""
        mock_load_cfg.return_value = {"cluster": {"cluster_name": "test-cluster", "modules": ["arc-runners"]}, "defaults": {}}
        mock_resolve.side_effect = lambda cfg, key, *args: {
            "cluster_name": "pytorch-test",
            "arc-runners.runner_name_prefix": "mt-",
        }.get(key, args[0] if args else None)
        mock_ensure_canary.return_value = Path("/tmp/canary")
        mock_ensure_pytorch.return_value = Path("/tmp/pytorch")
        mock_run_cmd.return_value = MagicMock(returncode=0)
        mock_create_pr.return_value = 42
        mock_monitor.return_value = [{"run_id": 1, "jobs": [{"name": "lint", "conclusion": "success"}]}]
        mock_report.return_value = True

        with patch("sys.argv", self._base_argv()):
            with pytest.raises(SystemExit) as exc:
                main()
            assert exc.value.code == 0

        # Verify phase execution order
        mock_cleanup.assert_called_once()
        mock_ensure_canary.assert_called_once()
        mock_ensure_pytorch.assert_called_once()
        assert mock_run_cmd.call_count == 2  # git fetch + git checkout
        mock_mirror.assert_called_once()
        mock_instrument.assert_called_once()
        mock_create_pr.assert_called_once()
        mock_monitor.assert_called_once()
        mock_report.assert_called_once()
        # PR should be closed (no --keep-pr)
        mock_close_pr.assert_called_once_with(42, branch="osdc-workload-test-test-cluster")

    @patch("workload_run.close_pr")
    @patch("workload_run.print_workload_report")
    @patch("workload_run.monitor_workflows")
    @patch("workload_run.create_workload_pr")
    @patch("workload_run.instrument_workflows")
    @patch("workload_run.mirror_content")
    @patch("workload_run.ensure_pytorch_repo")
    @patch("workload_run.ensure_canary_repo")
    @patch("workload_run.cleanup_stale_prs")
    @patch("workload_run.run_cmd")
    @patch("workload_run.resolve")
    @patch("workload_run.load_cluster_config")
    def test_dry_run_exits_early(
        self,
        mock_load_cfg,
        mock_resolve,
        mock_run_cmd,
        mock_cleanup,
        mock_ensure_canary,
        mock_ensure_pytorch,
        mock_mirror,
        mock_instrument,
        mock_create_pr,
        mock_monitor,
        mock_report,
        mock_close_pr,
    ):
        """With --dry-run, exits after create_workload_pr returns None."""
        mock_load_cfg.return_value = {"cluster": {"cluster_name": "t", "modules": ["arc-runners"]}, "defaults": {}}
        mock_resolve.side_effect = lambda cfg, key, *args: {
            "cluster_name": "pytorch-test",
            "arc-runners.runner_name_prefix": "mt-",
        }.get(key, args[0] if args else None)
        mock_ensure_canary.return_value = Path("/tmp/canary")
        mock_ensure_pytorch.return_value = Path("/tmp/pytorch")
        mock_run_cmd.return_value = MagicMock(returncode=0)
        mock_create_pr.return_value = None  # dry_run returns None

        with patch("sys.argv", self._base_argv(dry_run=True)):
            with pytest.raises(SystemExit) as exc:
                main()
            assert exc.value.code == 0

        # Pre-PR phases should still run
        mock_cleanup.assert_called_once()
        mock_ensure_canary.assert_called_once()
        mock_ensure_pytorch.assert_called_once()
        mock_mirror.assert_called_once()
        mock_instrument.assert_called_once()
        mock_create_pr.assert_called_once()
        # Post-PR phases should NOT run
        mock_monitor.assert_not_called()
        mock_report.assert_not_called()
        mock_close_pr.assert_not_called()

    @patch("workload_run.close_pr")
    @patch("workload_run.print_workload_report")
    @patch("workload_run.monitor_workflows")
    @patch("workload_run.create_workload_pr")
    @patch("workload_run.instrument_workflows")
    @patch("workload_run.mirror_content")
    @patch("workload_run.ensure_pytorch_repo")
    @patch("workload_run.ensure_canary_repo")
    @patch("workload_run.cleanup_stale_prs")
    @patch("workload_run.run_cmd")
    @patch("workload_run.resolve")
    @patch("workload_run.load_cluster_config")
    def test_pr_creation_failure_exits_1(
        self,
        mock_load_cfg,
        mock_resolve,
        mock_run_cmd,
        mock_cleanup,
        mock_ensure_canary,
        mock_ensure_pytorch,
        mock_mirror,
        mock_instrument,
        mock_create_pr,
        mock_monitor,
        mock_report,
        mock_close_pr,
    ):
        """When create_workload_pr returns None (non-dry-run), exit 1."""
        mock_load_cfg.return_value = {"cluster": {"cluster_name": "t", "modules": ["arc-runners"]}, "defaults": {}}
        mock_resolve.side_effect = lambda cfg, key, *args: {
            "cluster_name": "pytorch-test",
            "arc-runners.runner_name_prefix": "mt-",
        }.get(key, args[0] if args else None)
        mock_ensure_canary.return_value = Path("/tmp/canary")
        mock_ensure_pytorch.return_value = Path("/tmp/pytorch")
        mock_run_cmd.return_value = MagicMock(returncode=0)
        mock_create_pr.return_value = None  # PR creation failed

        with patch("sys.argv", self._base_argv()):
            with pytest.raises(SystemExit) as exc:
                main()
            assert exc.value.code == 1

        # No monitoring or reporting should happen
        mock_monitor.assert_not_called()
        mock_report.assert_not_called()
        mock_close_pr.assert_not_called()

    @patch("workload_run.close_pr")
    @patch("workload_run.print_workload_report")
    @patch("workload_run.monitor_workflows")
    @patch("workload_run.create_workload_pr")
    @patch("workload_run.instrument_workflows")
    @patch("workload_run.mirror_content")
    @patch("workload_run.ensure_pytorch_repo")
    @patch("workload_run.ensure_canary_repo")
    @patch("workload_run.cleanup_stale_prs")
    @patch("workload_run.run_cmd")
    @patch("workload_run.resolve")
    @patch("workload_run.load_cluster_config")
    def test_keyboard_interrupt_collects_partial_and_closes_pr(
        self,
        mock_load_cfg,
        mock_resolve,
        mock_run_cmd,
        mock_cleanup,
        mock_ensure_canary,
        mock_ensure_pytorch,
        mock_mirror,
        mock_instrument,
        mock_create_pr,
        mock_monitor,
        mock_report,
        mock_close_pr,
    ):
        """KeyboardInterrupt: partial results collected, PR closed, exit 1."""
        mock_load_cfg.return_value = {"cluster": {"cluster_name": "t", "modules": ["arc-runners"]}, "defaults": {}}
        mock_resolve.side_effect = lambda cfg, key, *args: {
            "cluster_name": "pytorch-test",
            "arc-runners.runner_name_prefix": "mt-",
        }.get(key, args[0] if args else None)
        mock_ensure_canary.return_value = Path("/tmp/canary")
        mock_ensure_pytorch.return_value = Path("/tmp/pytorch")
        # First run_cmd calls are for git fetch/checkout (normal flow),
        # then the interrupt handler calls run_cmd for gh run list.
        mock_run_cmd.return_value = MagicMock(returncode=0, stdout="[]")
        mock_create_pr.return_value = 99
        mock_monitor.side_effect = KeyboardInterrupt

        with patch("sys.argv", self._base_argv()):
            with pytest.raises(SystemExit) as exc:
                main()
            assert exc.value.code == 1

        # Partial report should be called with interrupted=True
        mock_report.assert_called_once()
        _, kwargs = mock_report.call_args
        assert kwargs.get("interrupted") is True

        # PR should be closed
        mock_close_pr.assert_called_once_with(99, branch="osdc-workload-test-test-cluster")

    @patch("workload_run.close_pr")
    @patch("workload_run.print_workload_report")
    @patch("workload_run.monitor_workflows")
    @patch("workload_run.create_workload_pr")
    @patch("workload_run.instrument_workflows")
    @patch("workload_run.mirror_content")
    @patch("workload_run.ensure_pytorch_repo")
    @patch("workload_run.ensure_canary_repo")
    @patch("workload_run.cleanup_stale_prs")
    @patch("workload_run.run_cmd")
    @patch("workload_run.resolve")
    @patch("workload_run.load_cluster_config")
    def test_keyboard_interrupt_with_gh_run_list_failure(
        self,
        mock_load_cfg,
        mock_resolve,
        mock_run_cmd,
        mock_cleanup,
        mock_ensure_canary,
        mock_ensure_pytorch,
        mock_mirror,
        mock_instrument,
        mock_create_pr,
        mock_monitor,
        mock_report,
        mock_close_pr,
    ):
        """KeyboardInterrupt when gh run list fails: empty partial results."""
        mock_load_cfg.return_value = {"cluster": {"cluster_name": "t", "modules": ["arc-runners"]}, "defaults": {}}
        mock_resolve.side_effect = lambda cfg, key, *args: {
            "cluster_name": "pytorch-test",
            "arc-runners.runner_name_prefix": "",
        }.get(key, args[0] if args else None)
        mock_ensure_canary.return_value = Path("/tmp/canary")
        mock_ensure_pytorch.return_value = Path("/tmp/pytorch")

        # git fetch/checkout succeed, then gh run list during interrupt fails
        def run_cmd_side_effect(cmd, **kwargs):
            if "gh" in cmd and "run" in cmd:
                return MagicMock(returncode=1, stdout="", stderr="error")
            return MagicMock(returncode=0, stdout="")

        mock_run_cmd.side_effect = run_cmd_side_effect
        mock_create_pr.return_value = 50
        mock_monitor.side_effect = KeyboardInterrupt

        with patch("sys.argv", self._base_argv()):
            with pytest.raises(SystemExit) as exc:
                main()
            assert exc.value.code == 1

        # Report called with empty partial results
        mock_report.assert_called_once()
        report_args = mock_report.call_args
        # Third positional arg is the results list
        assert report_args[0][2] == []

    @patch("workload_run.close_pr")
    @patch("workload_run.print_workload_report")
    @patch("workload_run.monitor_workflows")
    @patch("workload_run.create_workload_pr")
    @patch("workload_run.instrument_workflows")
    @patch("workload_run.mirror_content")
    @patch("workload_run.ensure_pytorch_repo")
    @patch("workload_run.ensure_canary_repo")
    @patch("workload_run.cleanup_stale_prs")
    @patch("workload_run.run_cmd")
    @patch("workload_run.resolve")
    @patch("workload_run.load_cluster_config")
    def test_keyboard_interrupt_with_nonempty_partial(
        self,
        mock_load_cfg,
        mock_resolve,
        mock_run_cmd,
        mock_cleanup,
        mock_ensure_canary,
        mock_ensure_pytorch,
        mock_mirror,
        mock_instrument,
        mock_create_pr,
        mock_monitor,
        mock_report,
        mock_close_pr,
    ):
        """KeyboardInterrupt with successful gh run list: partial results collected."""
        import json

        mock_load_cfg.return_value = {"cluster": {"cluster_name": "t", "modules": ["arc-runners"]}, "defaults": {}}
        mock_resolve.side_effect = lambda cfg, key, *args: {
            "cluster_name": "pytorch-test",
            "arc-runners.runner_name_prefix": "",
        }.get(key, args[0] if args else None)
        mock_ensure_canary.return_value = Path("/tmp/canary")
        mock_ensure_pytorch.return_value = Path("/tmp/pytorch")

        partial_runs = json.dumps([
            {
                "databaseId": 100,
                "status": "in_progress",
                "conclusion": None,
                "name": "lint",
                "createdAt": "2099-01-01T00:00:00Z",
            }
        ])

        def run_cmd_side_effect(cmd, **kwargs):
            if "gh" in cmd and "run" in cmd and "list" in cmd:
                return MagicMock(returncode=0, stdout=partial_runs)
            return MagicMock(returncode=0, stdout="")

        mock_run_cmd.side_effect = run_cmd_side_effect
        mock_create_pr.return_value = 77
        mock_monitor.side_effect = KeyboardInterrupt

        with (
            patch("phases_validation._filter_runs_by_time", return_value=[{"databaseId": 100}]),
            patch("phases_validation._collect_run_details", return_value=[{"run_id": 100}]),
            patch("sys.argv", self._base_argv()),
        ):
            with pytest.raises(SystemExit) as exc:
                main()
            assert exc.value.code == 1

        mock_report.assert_called_once()

    @patch("workload_run.close_pr")
    @patch("workload_run.print_workload_report")
    @patch("workload_run.monitor_workflows")
    @patch("workload_run.create_workload_pr")
    @patch("workload_run.instrument_workflows")
    @patch("workload_run.mirror_content")
    @patch("workload_run.ensure_pytorch_repo")
    @patch("workload_run.ensure_canary_repo")
    @patch("workload_run.cleanup_stale_prs")
    @patch("workload_run.run_cmd")
    @patch("workload_run.resolve")
    @patch("workload_run.load_cluster_config")
    def test_keep_pr_flag_skips_close(
        self,
        mock_load_cfg,
        mock_resolve,
        mock_run_cmd,
        mock_cleanup,
        mock_ensure_canary,
        mock_ensure_pytorch,
        mock_mirror,
        mock_instrument,
        mock_create_pr,
        mock_monitor,
        mock_report,
        mock_close_pr,
    ):
        """With --keep-pr, PR is not closed after test."""
        mock_load_cfg.return_value = {"cluster": {"cluster_name": "t", "modules": ["arc-runners"]}, "defaults": {}}
        mock_resolve.side_effect = lambda cfg, key, *args: {
            "cluster_name": "pytorch-test",
            "arc-runners.runner_name_prefix": "mt-",
        }.get(key, args[0] if args else None)
        mock_ensure_canary.return_value = Path("/tmp/canary")
        mock_ensure_pytorch.return_value = Path("/tmp/pytorch")
        mock_run_cmd.return_value = MagicMock(returncode=0)
        mock_create_pr.return_value = 42
        mock_monitor.return_value = [{"run_id": 1, "jobs": []}]
        mock_report.return_value = True

        with patch("sys.argv", self._base_argv(keep_pr=True)):
            with pytest.raises(SystemExit) as exc:
                main()
            assert exc.value.code == 0

        # PR should NOT be closed
        mock_close_pr.assert_not_called()

    @patch("workload_run.close_pr")
    @patch("workload_run.print_workload_report")
    @patch("workload_run.monitor_workflows")
    @patch("workload_run.create_workload_pr")
    @patch("workload_run.instrument_workflows")
    @patch("workload_run.mirror_content")
    @patch("workload_run.ensure_pytorch_repo")
    @patch("workload_run.ensure_canary_repo")
    @patch("workload_run.cleanup_stale_prs")
    @patch("workload_run.run_cmd")
    @patch("workload_run.resolve")
    @patch("workload_run.load_cluster_config")
    def test_failed_workflows_exit_1(
        self,
        mock_load_cfg,
        mock_resolve,
        mock_run_cmd,
        mock_cleanup,
        mock_ensure_canary,
        mock_ensure_pytorch,
        mock_mirror,
        mock_instrument,
        mock_create_pr,
        mock_monitor,
        mock_report,
        mock_close_pr,
    ):
        """When print_workload_report returns False, exit 1."""
        mock_load_cfg.return_value = {"cluster": {"cluster_name": "t", "modules": ["arc-runners"]}, "defaults": {}}
        mock_resolve.side_effect = lambda cfg, key, *args: {
            "cluster_name": "pytorch-test",
            "arc-runners.runner_name_prefix": "mt-",
        }.get(key, args[0] if args else None)
        mock_ensure_canary.return_value = Path("/tmp/canary")
        mock_ensure_pytorch.return_value = Path("/tmp/pytorch")
        mock_run_cmd.return_value = MagicMock(returncode=0)
        mock_create_pr.return_value = 10
        mock_monitor.return_value = [{"run_id": 1, "jobs": [{"name": "lint", "conclusion": "failure"}]}]
        mock_report.return_value = False  # test failed

        with patch("sys.argv", self._base_argv()):
            with pytest.raises(SystemExit) as exc:
                main()
            assert exc.value.code == 1

        # PR should still be closed
        mock_close_pr.assert_called_once_with(10, branch="osdc-workload-test-test-cluster")

    @patch("workload_run.close_pr")
    @patch("workload_run.print_workload_report")
    @patch("workload_run.monitor_workflows")
    @patch("workload_run.create_workload_pr")
    @patch("workload_run.instrument_workflows")
    @patch("workload_run.mirror_content")
    @patch("workload_run.ensure_pytorch_repo")
    @patch("workload_run.ensure_canary_repo")
    @patch("workload_run.cleanup_stale_prs")
    @patch("workload_run.run_cmd")
    @patch("workload_run.resolve")
    @patch("workload_run.load_cluster_config")
    def test_monitor_args_passed_correctly(
        self,
        mock_load_cfg,
        mock_resolve,
        mock_run_cmd,
        mock_cleanup,
        mock_ensure_canary,
        mock_ensure_pytorch,
        mock_mirror,
        mock_instrument,
        mock_create_pr,
        mock_monitor,
        mock_report,
        mock_close_pr,
    ):
        """Verify monitor_workflows receives correct branch, timeout, poll_interval."""
        mock_load_cfg.return_value = {"cluster": {"cluster_name": "t", "modules": ["arc-runners"]}, "defaults": {}}
        mock_resolve.side_effect = lambda cfg, key, *args: {
            "cluster_name": "pytorch-test",
            "arc-runners.runner_name_prefix": "",
        }.get(key, args[0] if args else None)
        mock_ensure_canary.return_value = Path("/tmp/canary")
        mock_ensure_pytorch.return_value = Path("/tmp/pytorch")
        mock_run_cmd.return_value = MagicMock(returncode=0)
        mock_create_pr.return_value = 5
        mock_monitor.return_value = []
        mock_report.return_value = True

        with patch("sys.argv", self._base_argv(timeout=45)):
            with pytest.raises(SystemExit):
                main()

        mock_monitor.assert_called_once()
        call_kwargs = mock_monitor.call_args
        assert call_kwargs[0][0] == "osdc-workload-test-test-cluster"  # branch
        assert call_kwargs[1]["timeout_minutes"] == 45
        assert call_kwargs[1]["poll_interval"] == DEFAULT_POLL_INTERVAL

    @patch("workload_run.close_pr")
    @patch("workload_run.print_workload_report")
    @patch("workload_run.monitor_workflows")
    @patch("workload_run.create_workload_pr")
    @patch("workload_run.instrument_workflows")
    @patch("workload_run.mirror_content")
    @patch("workload_run.ensure_pytorch_repo")
    @patch("workload_run.ensure_canary_repo")
    @patch("workload_run.cleanup_stale_prs")
    @patch("workload_run.run_cmd")
    @patch("workload_run.resolve")
    @patch("workload_run.load_cluster_config")
    def test_report_args_passed_correctly(
        self,
        mock_load_cfg,
        mock_resolve,
        mock_run_cmd,
        mock_cleanup,
        mock_ensure_canary,
        mock_ensure_pytorch,
        mock_mirror,
        mock_instrument,
        mock_create_pr,
        mock_monitor,
        mock_report,
        mock_close_pr,
    ):
        """Verify print_workload_report receives cluster_id, cluster_name, results."""
        mock_load_cfg.return_value = {"cluster": {"cluster_name": "t", "modules": ["arc-runners"]}, "defaults": {}}
        mock_resolve.side_effect = lambda cfg, key, *args: {
            "cluster_name": "pytorch-test",
            "arc-runners.runner_name_prefix": "",
        }.get(key, args[0] if args else None)
        mock_ensure_canary.return_value = Path("/tmp/canary")
        mock_ensure_pytorch.return_value = Path("/tmp/pytorch")
        mock_run_cmd.return_value = MagicMock(returncode=0)
        mock_create_pr.return_value = 5
        monitor_results = [{"run_id": 1, "jobs": []}]
        mock_monitor.return_value = monitor_results
        mock_report.return_value = True

        with patch("sys.argv", self._base_argv()):
            with pytest.raises(SystemExit):
                main()

        mock_report.assert_called_once_with(
            "test-cluster",
            "pytorch-test",
            monitor_results,
        )

    @patch("workload_run.close_pr")
    @patch("workload_run.print_workload_report")
    @patch("workload_run.monitor_workflows")
    @patch("workload_run.create_workload_pr")
    @patch("workload_run.instrument_workflows")
    @patch("workload_run.mirror_content")
    @patch("workload_run.ensure_pytorch_repo")
    @patch("workload_run.ensure_canary_repo")
    @patch("workload_run.cleanup_stale_prs")
    @patch("workload_run.run_cmd")
    @patch("workload_run.resolve")
    @patch("workload_run.load_cluster_config")
    def test_cleanup_stale_prs_called_with_correct_args(
        self,
        mock_load_cfg,
        mock_resolve,
        mock_run_cmd,
        mock_cleanup,
        mock_ensure_canary,
        mock_ensure_pytorch,
        mock_mirror,
        mock_instrument,
        mock_create_pr,
        mock_monitor,
        mock_report,
        mock_close_pr,
    ):
        """Phase 0 cleanup uses correct branch and PR_TITLE_PREFIX."""
        from workload_phases import PR_TITLE_PREFIX

        mock_load_cfg.return_value = {"cluster": {"cluster_name": "t", "modules": ["arc-runners"]}, "defaults": {}}
        mock_resolve.side_effect = lambda cfg, key, *args: {
            "cluster_name": "pytorch-test",
            "arc-runners.runner_name_prefix": "",
        }.get(key, args[0] if args else None)
        mock_ensure_canary.return_value = Path("/tmp/canary")
        mock_ensure_pytorch.return_value = Path("/tmp/pytorch")
        mock_run_cmd.return_value = MagicMock(returncode=0)
        mock_create_pr.return_value = None  # fail early

        with patch("sys.argv", self._base_argv()):
            with pytest.raises(SystemExit):
                main()

        mock_cleanup.assert_called_once_with(
            "osdc-workload-test-test-cluster",
            pr_title_prefix=PR_TITLE_PREFIX,
        )

    @patch("workload_run.close_pr")
    @patch("workload_run.print_workload_report")
    @patch("workload_run.monitor_workflows")
    @patch("workload_run.create_workload_pr")
    @patch("workload_run.instrument_workflows")
    @patch("workload_run.mirror_content")
    @patch("workload_run.ensure_pytorch_repo")
    @patch("workload_run.ensure_canary_repo")
    @patch("workload_run.cleanup_stale_prs")
    @patch("workload_run.run_cmd")
    @patch("workload_run.resolve")
    @patch("workload_run.load_cluster_config")
    def test_git_checkout_uses_canary_path(
        self,
        mock_load_cfg,
        mock_resolve,
        mock_run_cmd,
        mock_cleanup,
        mock_ensure_canary,
        mock_ensure_pytorch,
        mock_mirror,
        mock_instrument,
        mock_create_pr,
        mock_monitor,
        mock_report,
        mock_close_pr,
    ):
        """Git fetch and checkout are called with canary_path as cwd."""
        canary = Path("/tmp/test-canary")
        mock_load_cfg.return_value = {"cluster": {"cluster_name": "t", "modules": ["arc-runners"]}, "defaults": {}}
        mock_resolve.side_effect = lambda cfg, key, *args: {
            "cluster_name": "pytorch-test",
            "arc-runners.runner_name_prefix": "",
        }.get(key, args[0] if args else None)
        mock_ensure_canary.return_value = canary
        mock_ensure_pytorch.return_value = Path("/tmp/pytorch")
        mock_run_cmd.return_value = MagicMock(returncode=0)
        mock_create_pr.return_value = None  # fail early

        with patch("sys.argv", self._base_argv()):
            with pytest.raises(SystemExit):
                main()

        # Verify git fetch call
        fetch_call = mock_run_cmd.call_args_list[0]
        assert fetch_call[0][0] == ["git", "fetch", "origin", "main"]
        assert fetch_call[1]["cwd"] == canary

        # Verify git checkout call
        checkout_call = mock_run_cmd.call_args_list[1]
        assert checkout_call[0][0][:3] == ["git", "checkout", "-B"]
        assert "osdc-workload-test-test-cluster" in checkout_call[0][0]
        assert "origin/main" in checkout_call[0][0]
        assert checkout_call[1]["cwd"] == canary

    @patch("workload_run.close_pr")
    @patch("workload_run.print_workload_report")
    @patch("workload_run.monitor_workflows")
    @patch("workload_run.create_workload_pr")
    @patch("workload_run.instrument_workflows")
    @patch("workload_run.mirror_content")
    @patch("workload_run.ensure_pytorch_repo")
    @patch("workload_run.ensure_canary_repo")
    @patch("workload_run.cleanup_stale_prs")
    @patch("workload_run.run_cmd")
    @patch("workload_run.resolve")
    @patch("workload_run.load_cluster_config")
    def test_instrument_receives_prefix(
        self,
        mock_load_cfg,
        mock_resolve,
        mock_run_cmd,
        mock_cleanup,
        mock_ensure_canary,
        mock_ensure_pytorch,
        mock_mirror,
        mock_instrument,
        mock_create_pr,
        mock_monitor,
        mock_report,
        mock_close_pr,
    ):
        """instrument_workflows is called with canary_path and prefix."""
        canary = Path("/tmp/canary")
        mock_load_cfg.return_value = {"cluster": {"cluster_name": "t", "modules": ["arc-runners"]}, "defaults": {}}
        mock_resolve.side_effect = lambda cfg, key, *args: {
            "cluster_name": "pytorch-test",
            "arc-runners.runner_name_prefix": "cbr-",
        }.get(key, args[0] if args else None)
        mock_ensure_canary.return_value = canary
        mock_ensure_pytorch.return_value = Path("/tmp/pytorch")
        mock_run_cmd.return_value = MagicMock(returncode=0)
        mock_create_pr.return_value = None

        with patch("sys.argv", self._base_argv()):
            with pytest.raises(SystemExit):
                main()

        mock_instrument.assert_called_once_with(canary, "cbr-")

    @patch("workload_run.close_pr")
    @patch("workload_run.print_workload_report")
    @patch("workload_run.monitor_workflows")
    @patch("workload_run.create_workload_pr")
    @patch("workload_run.instrument_workflows")
    @patch("workload_run.mirror_content")
    @patch("workload_run.ensure_pytorch_repo")
    @patch("workload_run.ensure_canary_repo")
    @patch("workload_run.cleanup_stale_prs")
    @patch("workload_run.run_cmd")
    @patch("workload_run.resolve")
    @patch("workload_run.load_cluster_config")
    def test_mirror_receives_both_paths(
        self,
        mock_load_cfg,
        mock_resolve,
        mock_run_cmd,
        mock_cleanup,
        mock_ensure_canary,
        mock_ensure_pytorch,
        mock_mirror,
        mock_instrument,
        mock_create_pr,
        mock_monitor,
        mock_report,
        mock_close_pr,
    ):
        """mirror_content is called with pytorch_path and canary_path."""
        canary = Path("/tmp/canary")
        pytorch = Path("/tmp/pytorch")
        mock_load_cfg.return_value = {"cluster": {"cluster_name": "t", "modules": ["arc-runners"]}, "defaults": {}}
        mock_resolve.side_effect = lambda cfg, key, *args: {
            "cluster_name": "pytorch-test",
            "arc-runners.runner_name_prefix": "",
        }.get(key, args[0] if args else None)
        mock_ensure_canary.return_value = canary
        mock_ensure_pytorch.return_value = pytorch
        mock_run_cmd.return_value = MagicMock(returncode=0)
        mock_create_pr.return_value = None

        with patch("sys.argv", self._base_argv()):
            with pytest.raises(SystemExit):
                main()

        mock_mirror.assert_called_once_with(pytorch, canary)

    @patch("workload_run.close_pr")
    @patch("workload_run.print_workload_report")
    @patch("workload_run.monitor_workflows")
    @patch("workload_run.create_workload_pr")
    @patch("workload_run.instrument_workflows")
    @patch("workload_run.mirror_content")
    @patch("workload_run.ensure_pytorch_repo")
    @patch("workload_run.ensure_canary_repo")
    @patch("workload_run.cleanup_stale_prs")
    @patch("workload_run.run_cmd")
    @patch("workload_run.resolve")
    @patch("workload_run.load_cluster_config")
    def test_create_pr_receives_correct_args(
        self,
        mock_load_cfg,
        mock_resolve,
        mock_run_cmd,
        mock_cleanup,
        mock_ensure_canary,
        mock_ensure_pytorch,
        mock_mirror,
        mock_instrument,
        mock_create_pr,
        mock_monitor,
        mock_report,
        mock_close_pr,
    ):
        """create_workload_pr is called with canary_path, cluster_id, dry_run, branch."""
        canary = Path("/tmp/canary")
        mock_load_cfg.return_value = {"cluster": {"cluster_name": "t", "modules": ["arc-runners"]}, "defaults": {}}
        mock_resolve.side_effect = lambda cfg, key, *args: {
            "cluster_name": "pytorch-test",
            "arc-runners.runner_name_prefix": "",
        }.get(key, args[0] if args else None)
        mock_ensure_canary.return_value = canary
        mock_ensure_pytorch.return_value = Path("/tmp/pytorch")
        mock_run_cmd.return_value = MagicMock(returncode=0)
        mock_create_pr.return_value = None

        with patch("sys.argv", self._base_argv()):
            with pytest.raises(SystemExit):
                main()

        mock_create_pr.assert_called_once_with(
            canary,
            "test-cluster",
            False,  # dry_run=False
            "osdc-workload-test-test-cluster",
        )

    @patch("workload_run.close_pr")
    @patch("workload_run.print_workload_report")
    @patch("workload_run.monitor_workflows")
    @patch("workload_run.create_workload_pr")
    @patch("workload_run.instrument_workflows")
    @patch("workload_run.mirror_content")
    @patch("workload_run.ensure_pytorch_repo")
    @patch("workload_run.ensure_canary_repo")
    @patch("workload_run.cleanup_stale_prs")
    @patch("workload_run.run_cmd")
    @patch("workload_run.resolve")
    @patch("workload_run.load_cluster_config")
    def test_create_pr_dry_run_flag_forwarded(
        self,
        mock_load_cfg,
        mock_resolve,
        mock_run_cmd,
        mock_cleanup,
        mock_ensure_canary,
        mock_ensure_pytorch,
        mock_mirror,
        mock_instrument,
        mock_create_pr,
        mock_monitor,
        mock_report,
        mock_close_pr,
    ):
        """create_workload_pr receives dry_run=True when --dry-run is passed."""
        mock_load_cfg.return_value = {"cluster": {"cluster_name": "t", "modules": ["arc-runners"]}, "defaults": {}}
        mock_resolve.side_effect = lambda cfg, key, *args: {
            "cluster_name": "pytorch-test",
            "arc-runners.runner_name_prefix": "",
        }.get(key, args[0] if args else None)
        mock_ensure_canary.return_value = Path("/tmp/canary")
        mock_ensure_pytorch.return_value = Path("/tmp/pytorch")
        mock_run_cmd.return_value = MagicMock(returncode=0)
        mock_create_pr.return_value = None

        with patch("sys.argv", self._base_argv(dry_run=True)):
            with pytest.raises(SystemExit):
                main()

        mock_create_pr.assert_called_once_with(
            Path("/tmp/canary"),
            "test-cluster",
            True,  # dry_run=True
            "osdc-workload-test-test-cluster",
        )

    @patch("workload_run.close_pr")
    @patch("workload_run.print_workload_report")
    @patch("workload_run.monitor_workflows")
    @patch("workload_run.create_workload_pr")
    @patch("workload_run.instrument_workflows")
    @patch("workload_run.mirror_content")
    @patch("workload_run.ensure_pytorch_repo")
    @patch("workload_run.ensure_canary_repo")
    @patch("workload_run.cleanup_stale_prs")
    @patch("workload_run.run_cmd")
    @patch("workload_run.resolve")
    @patch("workload_run.load_cluster_config")
    def test_keep_pr_with_keyboard_interrupt(
        self,
        mock_load_cfg,
        mock_resolve,
        mock_run_cmd,
        mock_cleanup,
        mock_ensure_canary,
        mock_ensure_pytorch,
        mock_mirror,
        mock_instrument,
        mock_create_pr,
        mock_monitor,
        mock_report,
        mock_close_pr,
    ):
        """With --keep-pr and KeyboardInterrupt, PR is NOT closed."""
        mock_load_cfg.return_value = {"cluster": {"cluster_name": "t", "modules": ["arc-runners"]}, "defaults": {}}
        mock_resolve.side_effect = lambda cfg, key, *args: {
            "cluster_name": "pytorch-test",
            "arc-runners.runner_name_prefix": "",
        }.get(key, args[0] if args else None)
        mock_ensure_canary.return_value = Path("/tmp/canary")
        mock_ensure_pytorch.return_value = Path("/tmp/pytorch")
        mock_run_cmd.return_value = MagicMock(returncode=0, stdout="[]")
        mock_create_pr.return_value = 33
        mock_monitor.side_effect = KeyboardInterrupt

        with patch("sys.argv", self._base_argv(keep_pr=True)):
            with pytest.raises(SystemExit) as exc:
                main()
            assert exc.value.code == 1

        # PR should NOT be closed
        mock_close_pr.assert_not_called()

    @patch("workload_run.close_pr")
    @patch("workload_run.print_workload_report")
    @patch("workload_run.monitor_workflows")
    @patch("workload_run.create_workload_pr")
    @patch("workload_run.instrument_workflows")
    @patch("workload_run.mirror_content")
    @patch("workload_run.ensure_pytorch_repo")
    @patch("workload_run.ensure_canary_repo")
    @patch("workload_run.cleanup_stale_prs")
    @patch("workload_run.run_cmd")
    @patch("workload_run.resolve")
    @patch("workload_run.load_cluster_config")
    def test_resolve_empty_prefix(
        self,
        mock_load_cfg,
        mock_resolve,
        mock_run_cmd,
        mock_cleanup,
        mock_ensure_canary,
        mock_ensure_pytorch,
        mock_mirror,
        mock_instrument,
        mock_create_pr,
        mock_monitor,
        mock_report,
        mock_close_pr,
    ):
        """When runner_name_prefix is not set, resolve returns '' default."""
        mock_load_cfg.return_value = {"cluster": {"cluster_name": "t", "modules": ["arc-runners"]}, "defaults": {}}
        # Return "" for the prefix (the default), simulating missing config
        mock_resolve.side_effect = lambda cfg, key, *args: {
            "cluster_name": "pytorch-test",
        }.get(key, args[0] if args else None)
        mock_ensure_canary.return_value = Path("/tmp/canary")
        mock_ensure_pytorch.return_value = Path("/tmp/pytorch")
        mock_run_cmd.return_value = MagicMock(returncode=0)
        mock_create_pr.return_value = None

        with patch("sys.argv", self._base_argv()):
            with pytest.raises(SystemExit):
                main()

        # instrument_workflows should receive "" as prefix
        mock_instrument.assert_called_once_with(Path("/tmp/canary"), "")


# ── Constants ───────────────────────────────────────────────────────────


class TestConstants:
    def test_default_timeout_minutes(self):
        assert DEFAULT_TIMEOUT_MINUTES == 60

    def test_default_poll_interval(self):
        assert DEFAULT_POLL_INTERVAL == 30
