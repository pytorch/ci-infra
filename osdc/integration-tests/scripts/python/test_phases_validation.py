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
