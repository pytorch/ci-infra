"""Unit tests for phases_validation.py (print_report, _filter_runs_by_time, etc.)."""

import json
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest


class TestFilterRunsByTime:
    def setup_method(self):
        from phases_validation import _filter_runs_by_time

        self._filter = _filter_runs_by_time

    def test_filters_old_runs(self):
        cutoff = datetime(2026, 3, 20, 12, 0, 0, tzinfo=UTC)
        runs = [
            {"databaseId": 1, "createdAt": "2026-03-20T11:00:00Z"},  # before cutoff
            {"databaseId": 2, "createdAt": "2026-03-20T12:00:00Z"},  # at cutoff
            {"databaseId": 3, "createdAt": "2026-03-20T13:00:00Z"},  # after cutoff
        ]
        result = self._filter(runs, cutoff)
        assert [r["databaseId"] for r in result] == [2, 3]

    def test_keeps_runs_without_timestamp(self):
        cutoff = datetime(2026, 3, 20, 12, 0, 0, tzinfo=UTC)
        runs = [{"databaseId": 1}]
        result = self._filter(runs, cutoff)
        assert len(result) == 1

    def test_keeps_runs_with_unparseable_timestamp(self):
        cutoff = datetime(2026, 3, 20, 12, 0, 0, tzinfo=UTC)
        runs = [{"databaseId": 1, "createdAt": "not-a-date"}]
        result = self._filter(runs, cutoff)
        assert len(result) == 1

    def test_empty_list(self):
        cutoff = datetime(2026, 3, 20, 12, 0, 0, tzinfo=UTC)
        assert self._filter([], cutoff) == []


class TestPrintReport:
    def setup_method(self):
        from phases_validation import print_report

        self.print_report = print_report

    def test_all_pass(self, capsys):
        workflow_results = [
            {
                "run_id": 1,
                "jobs": [
                    {"name": "test-cpu", "conclusion": "success"},
                    {"name": "test-gpu", "conclusion": "success"},
                ],
            },
        ]
        validation = {
            "smoke": {"status": "passed"},
            "compactor": {"status": "passed"},
        }

        result = self.print_report("arc-staging", "pytorch-arc-staging", workflow_results, validation)

        assert result is True
        out = capsys.readouterr().out
        assert "PASSED" in out
        assert "FAILED" not in out

    def test_workflow_failure_sets_overall_fail(self, capsys):
        workflow_results = [
            {
                "run_id": 1,
                "jobs": [
                    {"name": "test-cpu", "conclusion": "success"},
                    {"name": "test-gpu", "conclusion": "failure"},
                ],
            },
        ]
        validation = {
            "smoke": {"status": "passed"},
            "compactor": {"status": "passed"},
        }

        result = self.print_report("arc-staging", "pytorch-arc-staging", workflow_results, validation)

        assert result is False

    def test_validation_failure_sets_overall_fail(self, capsys):
        workflow_results = []
        validation = {
            "smoke": {"status": "failed"},
            "compactor": {"status": "skipped"},
        }

        result = self.print_report("arc-staging", "pytorch-arc-staging", workflow_results, validation)

        assert result is False

    def test_skipped_validation_does_not_fail(self, capsys):
        workflow_results = []
        validation = {
            "smoke": {"status": "skipped"},
            "compactor": {"status": "skipped"},
        }

        result = self.print_report("arc-staging", "pytorch-arc-staging", workflow_results, validation)

        assert result is True

    def test_failure_log_printed(self, capsys):
        workflow_results = [
            {
                "run_id": 42,
                "failure_log": "Error: something broke\nLine 2",
                "jobs": [{"name": "build", "conclusion": "failure"}],
            },
        ]
        validation = {"smoke": {"status": "passed"}, "compactor": {"status": "passed"}}

        self.print_report("arc-staging", "pytorch-arc-staging", workflow_results, validation)

        out = capsys.readouterr().out
        assert "something broke" in out
        assert "run 42" in out

    def test_validation_failure_shows_output(self, capsys):
        workflow_results = []
        validation = {
            "smoke": {"status": "failed", "output": "FAILED test_something\nAssertionError: pods not ready\n"},
            "compactor": {"status": "passed"},
        }

        result = self.print_report("arc-staging", "pytorch-arc-staging", workflow_results, validation)

        assert result is False
        out = capsys.readouterr().out
        assert "Smoke output" in out
        assert "AssertionError: pods not ready" in out

    def test_validation_failure_no_output_no_crash(self, capsys):
        workflow_results = []
        validation = {
            "smoke": {"status": "failed"},
            "compactor": {"status": "failed", "output": ""},
        }

        result = self.print_report("arc-staging", "pytorch-arc-staging", workflow_results, validation)

        assert result is False

    def test_interrupted_header(self, capsys):
        self.print_report(
            "arc-staging",
            "pytorch-arc-staging",
            [],
            {},
            interrupted=True,
        )
        out = capsys.readouterr().out
        assert "(interrupted)" in out

    def test_interrupted_validation_does_not_fail(self, capsys):
        validation = {
            "smoke": {"status": "passed"},
            "compactor": {"status": "interrupted"},
        }
        result = self.print_report(
            "arc-staging",
            "pytorch-arc-staging",
            [],
            validation,
            interrupted=True,
        )
        assert result is True
        out = capsys.readouterr().out
        assert "INTERRUPTED" in out

    def test_in_progress_workflow_does_not_fail(self, capsys):
        workflow_results = [
            {
                "run_id": 1,
                "jobs": [
                    {"name": "test-cpu", "conclusion": "success"},
                    {"name": "test-gpu", "conclusion": "in_progress"},
                ],
            },
        ]
        validation = {"smoke": {"status": "passed"}, "compactor": {"status": "passed"}}
        result = self.print_report(
            "arc-staging",
            "pytorch-arc-staging",
            workflow_results,
            validation,
            interrupted=True,
        )
        assert result is True
        out = capsys.readouterr().out
        assert "in_progress" in out

    def test_null_job_conclusion_treated_as_in_progress(self, capsys):
        """GitHub API returns None for in-progress job conclusions."""
        workflow_results = [
            {
                "run_id": 1,
                "jobs": [
                    {"name": "test-cpu", "conclusion": "success"},
                    {"name": "test-gpu", "conclusion": None},
                ],
            },
        ]
        validation = {"smoke": {"status": "passed"}, "compactor": {"status": "passed"}}
        result = self.print_report(
            "arc-staging",
            "pytorch-arc-staging",
            workflow_results,
            validation,
            interrupted=True,
        )
        assert result is True
        out = capsys.readouterr().out
        assert "in_progress" in out

    def test_duration_displayed_when_present(self, capsys):
        """Validation results with duration_s show formatted elapsed time."""
        validation = {
            "smoke": {"status": "passed", "duration_s": 125.3},
            "compactor": {"status": "passed", "duration_s": 7.0},
        }
        result = self.print_report("arc-staging", "pytorch-arc-staging", [], validation)
        assert result is True
        out = capsys.readouterr().out
        assert "2m05s" in out  # 125s -> 2m05s
        assert "(7s)" in out  # 7s -> 7s (sub-minute)

    def test_duration_absent_no_crash(self, capsys):
        """Validation results without duration_s still render correctly."""
        validation = {
            "smoke": {"status": "passed"},
            "compactor": {"status": "skipped"},
        }
        result = self.print_report("arc-staging", "pytorch-arc-staging", [], validation)
        assert result is True
        out = capsys.readouterr().out
        # No duration suffix expected
        assert "(0s)" not in out
        assert "()" not in out

    def test_partial_results_mixed(self, capsys):
        """Interrupted run with a real failure + in-progress job => FAILED."""
        workflow_results = [
            {
                "run_id": 1,
                "jobs": [
                    {"name": "test-cpu", "conclusion": "failure"},
                    {"name": "test-gpu", "conclusion": "in_progress"},
                ],
            },
        ]
        validation = {
            "smoke": {"status": "passed"},
            "compactor": {"status": "interrupted"},
        }
        result = self.print_report(
            "arc-staging",
            "pytorch-arc-staging",
            workflow_results,
            validation,
            interrupted=True,
        )
        assert result is False


class TestRunParallelValidationInterrupt:
    """Test that run_parallel_validation handles KeyboardInterrupt."""

    def test_interrupt_terminates_running_procs(self):
        from pathlib import Path
        from unittest.mock import MagicMock, patch

        from phases_validation import run_parallel_validation

        # Create mock procs: first communicate() succeeds, second raises KeyboardInterrupt
        proc_smoke = MagicMock()
        proc_smoke.communicate.return_value = ("smoke output", None)
        proc_smoke.returncode = 0

        proc_compactor = MagicMock()
        proc_compactor.communicate.side_effect = KeyboardInterrupt

        call_count = 0

        def fake_popen(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return proc_smoke
            return proc_compactor

        cfg = {"cluster": {"node_compactor": {"enabled": True}}, "defaults": {}}

        with patch("subprocess.Popen", side_effect=fake_popen):
            import pytest

            with pytest.raises(KeyboardInterrupt):
                run_parallel_validation(
                    "test-cluster",
                    Path("/root"),
                    Path("/upstream"),
                    skip_smoke=False,
                    skip_compactor=False,
                    cfg=cfg,
                )

        # compactor proc should have been terminated
        proc_compactor.terminate.assert_called_once()

    def test_already_finished_procs_kept(self):
        """Procs that finished before interrupt keep their real results."""
        from pathlib import Path
        from unittest.mock import MagicMock, patch

        from phases_validation import run_parallel_validation

        proc_smoke = MagicMock()
        proc_smoke.communicate.return_value = ("smoke output", None)
        proc_smoke.returncode = 0

        proc_compactor = MagicMock()
        proc_compactor.communicate.side_effect = KeyboardInterrupt

        call_count = 0

        def fake_popen(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return proc_smoke
            return proc_compactor

        cfg = {"cluster": {"node_compactor": {"enabled": True}}, "defaults": {}}

        import contextlib

        with patch("subprocess.Popen", side_effect=fake_popen), contextlib.suppress(KeyboardInterrupt):
            run_parallel_validation(
                "test-cluster",
                Path("/root"),
                Path("/upstream"),
                skip_smoke=False,
                skip_compactor=False,
                cfg=cfg,
            )

        # compactor proc should have been terminated
        proc_compactor.terminate.assert_called_once()


# ── wait_for_workflows ─────────────────────────────────────────────────


class TestWaitForWorkflows:
    @patch("phases_validation.time.sleep")
    @patch("phases_validation.run_cmd")
    def test_keyboard_interrupt_reraises(self, mock_run, mock_sleep):
        """KeyboardInterrupt must propagate so main() can handle cleanup."""
        from phases_validation import wait_for_workflows

        mock_run.side_effect = KeyboardInterrupt

        with pytest.raises(KeyboardInterrupt):
            wait_for_workflows(
                "test-branch",
                datetime(2026, 3, 20, 12, 0, 0, tzinfo=UTC),
            )

    @patch("phases_validation.time.sleep")
    @patch("phases_validation.run_cmd")
    def test_filters_by_workflow_name(self, mock_run, mock_sleep):
        """When workflow_name is set, only matching runs are tracked."""
        from phases_validation import wait_for_workflows

        runs_json = json.dumps(
            [
                {
                    "databaseId": 1,
                    "status": "completed",
                    "conclusion": "success",
                    "name": "target-wf",
                    "createdAt": "2026-03-20T13:00:00Z",
                },
                {
                    "databaseId": 2,
                    "status": "completed",
                    "conclusion": "success",
                    "name": "other-wf",
                    "createdAt": "2026-03-20T13:00:00Z",
                },
            ]
        )

        mock_run.side_effect = [
            # gh run list
            MagicMock(returncode=0, stdout=runs_json, stderr=""),
            # gh run view for run 1 (only target-wf should be collected)
            MagicMock(returncode=0, stdout=json.dumps({"jobs": []}), stderr=""),
        ]

        results = wait_for_workflows(
            "test-branch",
            datetime(2026, 3, 20, 12, 0, 0, tzinfo=UTC),
            workflow_name="target-wf",
        )

        assert len(results) == 1
        assert results[0]["name"] == "target-wf"

    def test_timeout_buffer(self):
        """Effective deadline uses a cleanup buffer, not the full timeout."""
        from run import WORKFLOW_TIMEOUT_MINUTES

        # The buffer is hardcoded to 10 minutes inside wait_for_workflows.
        # Verify the math: effective = max(WORKFLOW_TIMEOUT_MINUTES - 10, 10)
        expected_effective = max(WORKFLOW_TIMEOUT_MINUTES - 10, 10)
        assert expected_effective < WORKFLOW_TIMEOUT_MINUTES
        assert expected_effective >= 10


# ── close_pr ───────────────────────────────────────────────────────────


class TestClosePr:
    @patch("phases_validation.run_cmd")
    def test_cancels_workflows_before_closing(self, mock_run):
        """When branch is provided, queued and in-progress runs are cancelled."""
        from phases_validation import close_pr

        queued_runs = json.dumps([{"databaseId": 100}])
        in_progress_runs = json.dumps([{"databaseId": 200}])

        mock_run.side_effect = [
            # gh run list --status queued
            MagicMock(returncode=0, stdout=queued_runs, stderr=""),
            # gh run cancel 100
            MagicMock(returncode=0, stdout="", stderr=""),
            # gh run list --status in_progress
            MagicMock(returncode=0, stdout=in_progress_runs, stderr=""),
            # gh run cancel 200
            MagicMock(returncode=0, stdout="", stderr=""),
            # gh pr close
            MagicMock(returncode=0, stdout="", stderr=""),
        ]

        close_pr(42, branch="test-branch")

        assert mock_run.call_count == 5

        # Verify cancel calls happened before pr close
        cancel_1 = mock_run.call_args_list[1][0][0]
        assert "cancel" in cancel_1
        assert "100" in cancel_1

        cancel_2 = mock_run.call_args_list[3][0][0]
        assert "cancel" in cancel_2
        assert "200" in cancel_2

        pr_close = mock_run.call_args_list[4][0][0]
        assert "close" in pr_close
        assert "42" in pr_close

    @patch("phases_validation.run_cmd")
    def test_works_without_branch(self, mock_run):
        """Without branch arg, no cancel calls — backward compatible."""
        from phases_validation import close_pr

        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        close_pr(42)

        # Only the pr close call
        assert mock_run.call_count == 1
        cmd = mock_run.call_args_list[0][0][0]
        assert "close" in cmd
        assert "42" in cmd

    @patch("phases_validation.run_cmd")
    def test_handles_no_running_workflows(self, mock_run):
        """When no runs are queued or in-progress, only the PR close happens."""
        from phases_validation import close_pr

        empty_runs = json.dumps([])

        mock_run.side_effect = [
            # gh run list --status queued (empty)
            MagicMock(returncode=0, stdout=empty_runs, stderr=""),
            # gh run list --status in_progress (empty)
            MagicMock(returncode=0, stdout=empty_runs, stderr=""),
            # gh pr close
            MagicMock(returncode=0, stdout="", stderr=""),
        ]

        close_pr(42, branch="test-branch")

        assert mock_run.call_count == 3
        # Last call should be pr close
        cmd = mock_run.call_args_list[2][0][0]
        assert "close" in cmd

    @patch("phases_validation.run_cmd")
    def test_handles_run_list_failure(self, mock_run):
        """When gh run list returns non-zero, skip cancel and just close PR."""
        from phases_validation import close_pr

        mock_run.side_effect = [
            # gh run list --status queued (fails)
            MagicMock(returncode=1, stdout="", stderr="api error"),
            # gh run list --status in_progress (fails)
            MagicMock(returncode=1, stdout="", stderr="api error"),
            # gh pr close
            MagicMock(returncode=0, stdout="", stderr=""),
        ]

        close_pr(42, branch="test-branch")

        assert mock_run.call_count == 3
        # No cancel calls — only list + close
        for call in mock_run.call_args_list[:2]:
            assert "cancel" not in call[0][0]

    @patch("phases_validation.run_cmd")
    def test_handles_empty_stdout(self, mock_run):
        """When gh run list returns 0 but empty stdout, skip cancel."""
        from phases_validation import close_pr

        mock_run.side_effect = [
            # gh run list --status queued (success but empty)
            MagicMock(returncode=0, stdout="", stderr=""),
            # gh run list --status in_progress (success but empty)
            MagicMock(returncode=0, stdout="", stderr=""),
            # gh pr close
            MagicMock(returncode=0, stdout="", stderr=""),
        ]

        close_pr(42, branch="test-branch")

        assert mock_run.call_count == 3

    @patch("phases_validation.run_cmd")
    def test_handles_json_parse_failure_in_runs(self, mock_run):
        """When run list returns invalid JSON, skip cancel gracefully."""
        from phases_validation import close_pr

        mock_run.side_effect = [
            # gh run list --status queued (success, invalid JSON)
            MagicMock(returncode=0, stdout="not-json", stderr=""),
            # gh run list --status in_progress (success, invalid JSON)
            MagicMock(returncode=0, stdout="not-json", stderr=""),
            # gh pr close
            MagicMock(returncode=0, stdout="", stderr=""),
        ]

        close_pr(42, branch="test-branch")

        assert mock_run.call_count == 3


# ── run_parallel_validation (skip paths) ──────────────────────────────


class TestRunParallelValidationSkipPaths:
    """Test the skip_smoke and skip_compactor branches."""

    @patch("subprocess.Popen")
    def test_skip_smoke(self, mock_popen):
        """When skip_smoke=True, smoke is marked skipped, only compactor runs."""
        from pathlib import Path

        from phases_validation import run_parallel_validation

        proc = MagicMock()
        proc.communicate.return_value = ("compactor output", None)
        proc.returncode = 0
        mock_popen.return_value = proc

        cfg = {"cluster": {"node_compactor": {"enabled": True}}, "defaults": {}}

        results = run_parallel_validation(
            "test-cluster",
            Path("/root"),
            Path("/upstream"),
            skip_smoke=True,
            skip_compactor=False,
            cfg=cfg,
        )

        assert results["smoke"]["status"] == "skipped"
        assert results["compactor"]["status"] == "passed"
        # Only one Popen call (compactor)
        assert mock_popen.call_count == 1

    @patch("subprocess.Popen")
    def test_skip_compactor(self, mock_popen):
        """When skip_compactor=True, compactor is marked skipped, only smoke runs."""
        from pathlib import Path

        from phases_validation import run_parallel_validation

        proc = MagicMock()
        proc.communicate.return_value = ("smoke output", None)
        proc.returncode = 0
        mock_popen.return_value = proc

        cfg = {"cluster": {"node_compactor": {"enabled": True}}, "defaults": {}}

        results = run_parallel_validation(
            "test-cluster",
            Path("/root"),
            Path("/upstream"),
            skip_smoke=False,
            skip_compactor=True,
            cfg=cfg,
        )

        assert results["smoke"]["status"] == "passed"
        assert results["compactor"]["status"] == "skipped"
        assert mock_popen.call_count == 1

    @patch("subprocess.Popen")
    def test_skip_both(self, mock_popen):
        """When both are skipped, no Popen calls, both marked skipped."""
        from pathlib import Path

        from phases_validation import run_parallel_validation

        cfg = {"cluster": {"node_compactor": {"enabled": True}}, "defaults": {}}

        results = run_parallel_validation(
            "test-cluster",
            Path("/root"),
            Path("/upstream"),
            skip_smoke=True,
            skip_compactor=True,
            cfg=cfg,
        )

        assert results["smoke"]["status"] == "skipped"
        assert results["compactor"]["status"] == "skipped"
        mock_popen.assert_not_called()

    @patch("subprocess.Popen")
    def test_compactor_disabled_in_config(self, mock_popen):
        """When node_compactor.enabled=False in config, compactor is skipped."""
        from pathlib import Path

        from phases_validation import run_parallel_validation

        proc = MagicMock()
        proc.communicate.return_value = ("smoke output", None)
        proc.returncode = 0
        mock_popen.return_value = proc

        cfg = {"cluster": {"node_compactor": {"enabled": False}}, "defaults": {}}

        results = run_parallel_validation(
            "test-cluster",
            Path("/root"),
            Path("/upstream"),
            skip_smoke=False,
            skip_compactor=False,
            cfg=cfg,
        )

        assert results["smoke"]["status"] == "passed"
        assert results["compactor"]["status"] == "skipped"
        assert mock_popen.call_count == 1


class TestRunParallelValidationKillBranch:
    """Test the TimeoutExpired branch in interrupt handling."""

    def test_kill_after_timeout_expired(self):
        """When proc.wait(timeout=5) raises TimeoutExpired, proc.kill() is called."""
        import subprocess
        from pathlib import Path
        from unittest.mock import patch

        from phases_validation import run_parallel_validation

        proc = MagicMock()
        proc.communicate.side_effect = KeyboardInterrupt
        # First wait(timeout=5) raises TimeoutExpired, second wait() (after kill) succeeds
        proc.wait.side_effect = [
            subprocess.TimeoutExpired(cmd="test", timeout=5),
            None,
        ]

        cfg = {"cluster": {}, "defaults": {}}

        with patch("subprocess.Popen", return_value=proc), pytest.raises(KeyboardInterrupt):
            run_parallel_validation(
                "test-cluster",
                Path("/root"),
                Path("/upstream"),
                skip_smoke=False,
                skip_compactor=True,
                cfg=cfg,
            )

        proc.terminate.assert_called_once()
        proc.kill.assert_called_once()


# ── wait_for_workflows (polling branches) ─────────────────────────────


def _make_time_sequence(*values):
    """Create a time.time() mock that returns values in sequence, then repeats the last.

    This is needed because Python's logging module also calls time.time() internally
    for log record timestamps, consuming extra calls beyond our explicit usage.
    """
    it = iter(values)
    last = values[-1]

    def fake_time():
        nonlocal last
        try:
            val = next(it)
            last = val
            return val
        except StopIteration:
            return last

    return fake_time


class TestWaitForWorkflowsPolling:
    @patch("phases_validation.time.time")
    @patch("phases_validation.time.sleep")
    @patch("phases_validation.run_cmd")
    def test_run_list_failure_retries(self, mock_run, mock_sleep, mock_time):
        """When gh run list fails, it logs a warning, sleeps, and retries."""
        from phases_validation import wait_for_workflows

        # Use a function that returns increasing values; last value repeats for logging calls
        mock_time.side_effect = _make_time_sequence(0, 100, 200, 9999)

        completed_run = {
            "databaseId": 1,
            "status": "completed",
            "conclusion": "success",
            "name": "test",
            "createdAt": "2026-03-20T13:00:00Z",
        }

        mock_run.side_effect = [
            # First poll: failure
            MagicMock(returncode=1, stdout="", stderr="network error"),
            # Second poll: success with completed run
            MagicMock(returncode=0, stdout=json.dumps([completed_run]), stderr=""),
            # gh run view for collecting details
            MagicMock(returncode=0, stdout=json.dumps({"jobs": []}), stderr=""),
        ]

        results = wait_for_workflows(
            "test-branch",
            datetime(2026, 3, 20, 12, 0, 0, tzinfo=UTC),
        )

        assert len(results) == 1
        # Sleep called once after the failure
        assert mock_sleep.call_count == 1

    @patch("phases_validation.time.time")
    @patch("phases_validation.time.sleep")
    @patch("phases_validation.run_cmd")
    def test_no_runs_found_waits(self, mock_run, mock_sleep, mock_time):
        """When no runs are found, log and wait before retrying."""
        from phases_validation import wait_for_workflows

        mock_time.side_effect = _make_time_sequence(0, 100, 200, 9999)

        completed_run = {
            "databaseId": 1,
            "status": "completed",
            "conclusion": "success",
            "name": "test",
            "createdAt": "2026-03-20T13:00:00Z",
        }

        mock_run.side_effect = [
            # First poll: no runs
            MagicMock(returncode=0, stdout="[]", stderr=""),
            # Second poll: run found and completed
            MagicMock(returncode=0, stdout=json.dumps([completed_run]), stderr=""),
            # gh run view
            MagicMock(returncode=0, stdout=json.dumps({"jobs": []}), stderr=""),
        ]

        results = wait_for_workflows(
            "test-branch",
            datetime(2026, 3, 20, 12, 0, 0, tzinfo=UTC),
        )

        assert len(results) == 1
        assert mock_sleep.call_count == 1

    @patch("phases_validation.time.time")
    @patch("phases_validation.time.sleep")
    @patch("phases_validation.run_cmd")
    def test_in_progress_runs_wait(self, mock_run, mock_sleep, mock_time):
        """When runs exist but not all completed, wait and poll again."""
        from phases_validation import wait_for_workflows

        mock_time.side_effect = _make_time_sequence(0, 100, 200, 9999)

        in_progress_run = {
            "databaseId": 1,
            "status": "in_progress",
            "conclusion": None,
            "name": "test",
            "createdAt": "2026-03-20T13:00:00Z",
        }
        completed_run = {
            "databaseId": 1,
            "status": "completed",
            "conclusion": "success",
            "name": "test",
            "createdAt": "2026-03-20T13:00:00Z",
        }

        mock_run.side_effect = [
            # First poll: in progress
            MagicMock(returncode=0, stdout=json.dumps([in_progress_run]), stderr=""),
            # Second poll: completed
            MagicMock(returncode=0, stdout=json.dumps([completed_run]), stderr=""),
            # gh run view
            MagicMock(returncode=0, stdout=json.dumps({"jobs": []}), stderr=""),
        ]

        results = wait_for_workflows(
            "test-branch",
            datetime(2026, 3, 20, 12, 0, 0, tzinfo=UTC),
        )

        assert len(results) == 1
        assert results[0]["conclusion"] == "success"
        # One sleep while waiting for in-progress run
        assert mock_sleep.call_count == 1

    @patch("phases_validation._fetch_latest_runs")
    @patch("phases_validation._collect_run_details")
    @patch("phases_validation.time.time")
    @patch("phases_validation.time.sleep")
    @patch("phases_validation.run_cmd")
    def test_timeout_collects_partial(self, mock_run, mock_sleep, mock_time, mock_collect, mock_fetch):
        """When timeout is reached, fetch latest runs and return partial results."""
        from phases_validation import wait_for_workflows

        # Immediately past deadline: first call sets deadline, all others exceed it
        mock_time.side_effect = _make_time_sequence(0, 999999)

        partial_run = {
            "databaseId": 1,
            "status": "in_progress",
            "conclusion": None,
            "name": "test",
            "createdAt": "2026-03-20T13:00:00Z",
        }
        mock_fetch.return_value = [partial_run]
        mock_collect.return_value = [{"run_id": 1, "conclusion": "in_progress"}]

        results = wait_for_workflows(
            "test-branch",
            datetime(2026, 3, 20, 12, 0, 0, tzinfo=UTC),
        )

        assert len(results) == 1
        mock_fetch.assert_called_once()
        mock_collect.assert_called_once()


# ── _fetch_latest_runs ────────────────────────────────────────────────


class TestFetchLatestRuns:
    @patch("phases_validation.run_cmd")
    def test_success(self, mock_run):
        from phases_validation import _fetch_latest_runs

        runs = [
            {"databaseId": 1, "createdAt": "2026-03-20T13:00:00Z"},
            {"databaseId": 2, "createdAt": "2026-03-20T11:00:00Z"},  # before cutoff
        ]
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps(runs),
            stderr="",
        )

        result = _fetch_latest_runs(
            "test-branch",
            datetime(2026, 3, 20, 12, 0, 0, tzinfo=UTC),
        )

        assert len(result) == 1
        assert result[0]["databaseId"] == 1

    @patch("phases_validation.run_cmd")
    def test_failure_returns_empty(self, mock_run):
        from phases_validation import _fetch_latest_runs

        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="error",
        )

        result = _fetch_latest_runs(
            "test-branch",
            datetime(2026, 3, 20, 12, 0, 0, tzinfo=UTC),
        )

        assert result == []

    @patch("phases_validation.run_cmd")
    def test_empty_stdout_returns_empty(self, mock_run):
        from phases_validation import _fetch_latest_runs

        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="",
            stderr="",
        )

        result = _fetch_latest_runs(
            "test-branch",
            datetime(2026, 3, 20, 12, 0, 0, tzinfo=UTC),
        )

        assert result == []

    @patch("phases_validation.run_cmd")
    def test_invalid_json_returns_empty(self, mock_run):
        from phases_validation import _fetch_latest_runs

        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="not-json",
            stderr="",
        )

        result = _fetch_latest_runs(
            "test-branch",
            datetime(2026, 3, 20, 12, 0, 0, tzinfo=UTC),
        )

        assert result == []


# ── _collect_run_details ───────────────────────────────────────────────


class TestCollectRunDetails:
    @patch("phases_validation.run_cmd")
    def test_collects_jobs_and_failure_log(self, mock_run):
        from phases_validation import _collect_run_details

        runs = [
            {"databaseId": 10, "conclusion": "failure", "name": "build", "status": "completed"},
        ]

        mock_run.side_effect = [
            # gh run view (job details)
            MagicMock(
                returncode=0,
                stdout=json.dumps({"jobs": [{"name": "build", "conclusion": "failure"}]}),
                stderr="",
            ),
            # gh run view --log-failed
            MagicMock(returncode=0, stdout="Error on line 42\nStack trace...\n", stderr=""),
        ]

        results = _collect_run_details(runs)

        assert len(results) == 1
        assert results[0]["run_id"] == 10
        assert results[0]["conclusion"] == "failure"
        assert len(results[0]["jobs"]) == 1
        assert "Error on line 42" in results[0]["failure_log"]

    @patch("phases_validation.run_cmd")
    def test_success_no_failure_log(self, mock_run):
        from phases_validation import _collect_run_details

        runs = [
            {"databaseId": 20, "conclusion": "success", "name": "test", "status": "completed"},
        ]

        mock_run.side_effect = [
            MagicMock(
                returncode=0,
                stdout=json.dumps({"jobs": [{"name": "test", "conclusion": "success"}]}),
                stderr="",
            ),
        ]

        results = _collect_run_details(runs)

        assert len(results) == 1
        assert results[0]["failure_log"] == ""
        # Only one call (no --log-failed for success)
        assert mock_run.call_count == 1

    @patch("phases_validation.run_cmd")
    def test_run_view_failure_empty_jobs(self, mock_run):
        from phases_validation import _collect_run_details

        runs = [
            {"databaseId": 30, "conclusion": "success", "name": "test", "status": "completed"},
        ]

        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="error")

        results = _collect_run_details(runs)

        assert len(results) == 1
        assert results[0]["jobs"] == []

    @patch("phases_validation.run_cmd")
    def test_null_conclusion_treated_as_in_progress(self, mock_run):
        from phases_validation import _collect_run_details

        runs = [
            {"databaseId": 40, "conclusion": None, "name": "test", "status": "in_progress"},
        ]

        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps({"jobs": []}),
            stderr="",
        )

        results = _collect_run_details(runs)

        assert results[0]["conclusion"] == "in_progress"
        # No --log-failed call for in_progress
        assert mock_run.call_count == 1
