"""Unit tests for phases_validation.py (print_report, _filter_runs_by_time, etc.)."""

from datetime import UTC, datetime


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
        result = self.print_report(
            "arc-staging", "pytorch-arc-staging", [], {}, interrupted=True,
        )
        out = capsys.readouterr().out
        assert "(interrupted)" in out

    def test_interrupted_validation_does_not_fail(self, capsys):
        validation = {
            "smoke": {"status": "passed"},
            "compactor": {"status": "interrupted"},
        }
        result = self.print_report(
            "arc-staging", "pytorch-arc-staging", [], validation, interrupted=True,
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
            "arc-staging", "pytorch-arc-staging", workflow_results, validation, interrupted=True,
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
            "arc-staging", "pytorch-arc-staging", workflow_results, validation, interrupted=True,
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
            "arc-staging", "pytorch-arc-staging", workflow_results, validation, interrupted=True,
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
                    "test-cluster", Path("/root"), Path("/upstream"),
                    skip_smoke=False, skip_compactor=False, cfg=cfg,
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

        with patch("subprocess.Popen", side_effect=fake_popen):
            import pytest

            try:
                run_parallel_validation(
                    "test-cluster", Path("/root"), Path("/upstream"),
                    skip_smoke=False, skip_compactor=False, cfg=cfg,
                )
            except KeyboardInterrupt:
                pass

        # compactor proc should have been terminated
        proc_compactor.terminate.assert_called_once()
