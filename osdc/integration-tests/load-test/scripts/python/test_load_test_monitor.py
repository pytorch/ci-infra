"""Unit tests for load test monitoring and reporting."""

import json
from datetime import UTC, datetime
from io import StringIO
from unittest.mock import MagicMock, patch

from distribution import RunnerAllocation
from load_test_monitor import (
    JobResult,
    LoadTestResults,
    _build_label_lookup,
    _collect_job_results,
    _get_filtered_runs,
    _print_progress,
    parse_runner_type,
    print_load_test_report,
    wait_for_load_test,
)
from workflow_generator import sanitize_job_key


# ── parse_runner_type ────────────────────────────────────────────────────


class TestParseRunnerType:
    def test_simple_label(self):
        assert parse_runner_type("load-l-x86iavx512-8-16 (42)") == "l-x86iavx512-8-16"

    def test_no_matrix_index(self):
        assert parse_runner_type("load-l-x86iavx512-8-16") == "l-x86iavx512-8-16"

    def test_gpu_label(self):
        assert parse_runner_type("load-l-x86iavx512-29-115-t4 (1)") == "l-x86iavx512-29-115-t4"

    def test_gpu_multi(self):
        assert parse_runner_type("load-l-x86iavx512-45-172-t4-4 (3)") == "l-x86iavx512-45-172-t4-4"

    def test_arm64(self):
        assert parse_runner_type("load-l-arm64g3-16-62 (10)") == "l-arm64g3-16-62"

    def test_non_load_test_job(self):
        assert parse_runner_type("build-image") is None

    def test_empty_string(self):
        assert parse_runner_type("") is None

    def test_split_part(self):
        assert parse_runner_type("load-l-x86iavx512-8-16-part0 (1)") == "l-x86iavx512-8-16-part0"


# ── _build_label_lookup ──────────────────────────────────────────────────


class TestBuildLabelLookup:
    def test_osdc_label(self):
        allocs = [
            RunnerAllocation("l-x86iavx512-8-16", 10, 0, 0.5, False, False, 0),
        ]
        lookup = _build_label_lookup(allocs)
        assert lookup["l-x86iavx512-8-16"] == "l-x86iavx512-8-16"

    def test_part_suffixed_lookup(self):
        allocs = [
            RunnerAllocation("l-x86iavx512-8-16", 300, 0, 0.5, False, False, 0),
        ]
        lookup = _build_label_lookup(allocs)
        # Part-suffixed keys should map back to the original label
        assert lookup["l-x86iavx512-8-16-part0"] == "l-x86iavx512-8-16"
        assert lookup["l-x86iavx512-8-16-part1"] == "l-x86iavx512-8-16"


# ── print_load_test_report ───────────────────────────────────────────────


class TestPrintLoadTestReport:
    def _make_results(self, jobs, timed_out=False):
        return LoadTestResults(
            total_expected=len(jobs),
            completed_jobs=len(jobs),
            timed_out=timed_out,
            duration_seconds=120.0,
            jobs=jobs,
            run_ids=[1],
        )

    def test_all_pass(self, capsys):
        jobs = [
            JobResult("load-l-x86iavx512-8-16 (1)", "success", "l-x86iavx512-8-16"),
            JobResult("load-l-x86iavx512-8-16 (2)", "success", "l-x86iavx512-8-16"),
            JobResult("load-l-arm64g3-16-62 (1)", "success", "l-arm64g3-16-62"),
        ]
        result = print_load_test_report("test", "test-cluster", self._make_results(jobs))
        assert result is True

        captured = capsys.readouterr()
        assert "PASSED" in captured.out
        assert "FAILED" not in captured.out

    def test_some_failures(self, capsys):
        jobs = [
            JobResult("load-l-x86iavx512-8-16 (1)", "success", "l-x86iavx512-8-16"),
            JobResult("load-l-x86iavx512-8-16 (2)", "failure", "l-x86iavx512-8-16"),
        ]
        result = print_load_test_report("test", "test-cluster", self._make_results(jobs))
        assert result is False

        captured = capsys.readouterr()
        assert "FAILED" in captured.out

    def test_timeout_fails(self, capsys):
        jobs = [
            JobResult("load-l-x86iavx512-8-16 (1)", "success", "l-x86iavx512-8-16"),
        ]
        result = print_load_test_report(
            "test",
            "test-cluster",
            self._make_results(jobs, timed_out=True),
        )
        assert result is False

        captured = capsys.readouterr()
        assert "timed out" in captured.out

    def test_empty_results(self, capsys):
        result = print_load_test_report(
            "test",
            "test-cluster",
            self._make_results([]),
        )
        # No jobs = pass (edge case, but no failures)
        assert result is True

    def test_failed_job_details(self, capsys):
        jobs = [
            JobResult("load-l-x86iavx512-8-16 (1)", "failure", "l-x86iavx512-8-16"),
        ]
        print_load_test_report("test", "test-cluster", self._make_results(jobs))

        captured = capsys.readouterr()
        assert "Failed jobs" in captured.out
        assert "load-l-x86iavx512-8-16 (1)" in captured.out

    def test_report_shows_duration(self, capsys):
        results = LoadTestResults(
            total_expected=1,
            completed_jobs=1,
            timed_out=False,
            duration_seconds=3661.0,
            jobs=[JobResult("load-x (1)", "success", "x")],
            run_ids=[1],
        )
        print_load_test_report("test", "test-cluster", results)

        captured = capsys.readouterr()
        assert "61m01s" in captured.out

    def test_many_failures_truncated(self, capsys):
        """More than 20 failed jobs should be truncated in the report."""
        jobs = [JobResult(f"load-x ({i})", "failure", "x") for i in range(25)]
        print_load_test_report("test", "test-cluster", self._make_results(jobs))

        captured = capsys.readouterr()
        assert "and 5 more" in captured.out


# ── _get_filtered_runs ─────────────────────────────────────────────────


class TestGetFilteredRuns:
    @patch("load_test_monitor.run_cmd")
    def test_filters_by_creation_time(self, mock_run_cmd):
        not_before = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        mock_run_cmd.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps(
                [
                    {"databaseId": 1, "createdAt": "2026-01-01T13:00:00Z", "status": "completed"},
                    {"databaseId": 2, "createdAt": "2025-12-31T10:00:00Z", "status": "completed"},
                ]
            ),
        )
        runs = _get_filtered_runs("test-branch", not_before)
        assert len(runs) == 1
        assert runs[0]["databaseId"] == 1

    @patch("load_test_monitor.run_cmd")
    def test_command_failure_returns_empty(self, mock_run_cmd):
        mock_run_cmd.return_value = MagicMock(
            returncode=1,
            stderr="error",
            stdout="",
        )
        runs = _get_filtered_runs("branch", datetime.now(tz=UTC))
        assert runs == []

    @patch("load_test_monitor.run_cmd")
    def test_empty_stdout_returns_empty(self, mock_run_cmd):
        mock_run_cmd.return_value = MagicMock(
            returncode=0,
            stdout="  ",
        )
        runs = _get_filtered_runs("branch", datetime.now(tz=UTC))
        assert runs == []

    @patch("load_test_monitor.run_cmd")
    def test_missing_created_at_included(self, mock_run_cmd):
        mock_run_cmd.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps(
                [
                    {"databaseId": 1, "createdAt": ""},
                    {"databaseId": 2},
                ]
            ),
        )
        runs = _get_filtered_runs("branch", datetime.now(tz=UTC))
        # Both should be included (missing/empty createdAt is not filtered out)
        assert len(runs) == 2

    @patch("load_test_monitor.run_cmd")
    def test_invalid_created_at_included(self, mock_run_cmd):
        mock_run_cmd.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps(
                [
                    {"databaseId": 1, "createdAt": "not-a-date"},
                ]
            ),
        )
        runs = _get_filtered_runs("branch", datetime.now(tz=UTC))
        assert len(runs) == 1


# ── _collect_job_results ───────────────────────────────────────────────


class TestCollectJobResults:
    @patch("load_test_monitor.run_cmd")
    def test_collects_load_test_jobs(self, mock_run_cmd):
        mock_run_cmd.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps(
                {
                    "jobs": [
                        {"name": "load-l-x86iavx512-8-16 (1)", "conclusion": "success"},
                        {"name": "load-l-x86iavx512-8-16 (2)", "conclusion": "failure"},
                    ],
                }
            ),
        )
        lookup = {"l-x86iavx512-8-16": "l-x86iavx512-8-16"}
        runs = [{"databaseId": 100}]
        jobs, run_ids = _collect_job_results(runs, lookup)
        assert len(jobs) == 2
        assert run_ids == [100]
        assert jobs[0].conclusion == "success"
        assert jobs[1].conclusion == "failure"

    @patch("load_test_monitor.run_cmd")
    def test_skips_non_load_test_jobs(self, mock_run_cmd):
        mock_run_cmd.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps(
                {
                    "jobs": [
                        {"name": "build-image", "conclusion": "success"},
                        {"name": "load-l-x86iavx512-8-16 (1)", "conclusion": "success"},
                    ],
                }
            ),
        )
        lookup = {"l-x86iavx512-8-16": "l-x86iavx512-8-16"}
        jobs, _ = _collect_job_results([{"databaseId": 1}], lookup)
        assert len(jobs) == 1
        assert jobs[0].name == "load-l-x86iavx512-8-16 (1)"

    @patch("load_test_monitor.run_cmd")
    def test_handles_command_failure(self, mock_run_cmd):
        mock_run_cmd.return_value = MagicMock(
            returncode=1,
            stdout="",
        )
        jobs, run_ids = _collect_job_results([{"databaseId": 1}], {})
        assert jobs == []
        assert run_ids == [1]

    @patch("load_test_monitor.run_cmd")
    def test_unknown_runner_key_uses_key(self, mock_run_cmd):
        mock_run_cmd.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps(
                {
                    "jobs": [
                        {"name": "load-unknown-runner (1)", "conclusion": "success"},
                    ],
                }
            ),
        )
        jobs, _ = _collect_job_results([{"databaseId": 1}], {})
        assert len(jobs) == 1
        assert jobs[0].runner_type == "unknown-runner"

    @patch("load_test_monitor.run_cmd")
    def test_missing_conclusion_defaults_unknown(self, mock_run_cmd):
        mock_run_cmd.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps(
                {
                    "jobs": [{"name": "load-x (1)"}],
                }
            ),
        )
        jobs, _ = _collect_job_results([{"databaseId": 1}], {})
        assert jobs[0].conclusion == "unknown"


# ── _print_progress ────────────────────────────────────────────────────


class TestPrintProgress:
    def test_logs_progress(self, caplog):
        import logging

        with caplog.at_level(logging.INFO, logger="osdc-load-test"):
            runs = [
                {"status": "completed"},
                {"status": "in_progress"},
                {"status": "in_progress"},
            ]
            _print_progress(runs, {}, 10)
        assert "1/3 runs completed" in caplog.text
        assert "2 in progress" in caplog.text


# ── wait_for_load_test ─────────────────────────────────────────────────


class TestWaitForLoadTest:
    def _alloc(self, label="l-x86iavx512-8-16", jobs=2):
        return RunnerAllocation(label, jobs, 1000, 0.5, False, False, 0)

    @patch("load_test_monitor._collect_job_results")
    @patch("load_test_monitor._get_filtered_runs")
    @patch("load_test_monitor.time")
    def test_all_completed(self, mock_time, mock_get_runs, mock_collect):
        # time.time() calls: deadline(79), start_time(80), while-check(88), duration(104)
        mock_time.time.side_effect = [100.0, 100.0, 100.0, 160.0]
        mock_time.sleep = MagicMock()

        mock_get_runs.return_value = [
            {"databaseId": 1, "status": "completed"},
        ]
        mock_collect.return_value = (
            [JobResult("load-l-x86iavx512-8-16 (1)", "success", "l-x86iavx512-8-16")],
            [1],
        )

        allocs = [self._alloc(jobs=1)]
        result = wait_for_load_test(
            "branch",
            datetime.now(tz=UTC),
            allocs,
            timeout_minutes=10,
        )
        assert result.timed_out is False
        assert result.completed_jobs == 1
        assert result.total_expected == 1

    @patch("load_test_monitor._collect_job_results")
    @patch("load_test_monitor._get_filtered_runs")
    @patch("load_test_monitor.time")
    def test_timeout(self, mock_time, mock_get_runs, mock_collect):
        # time.time() calls: deadline(79), start_time(80), while-check(88) past deadline, duration(120)
        mock_time.time.side_effect = [100.0, 100.0, 99999.0, 99999.0]
        mock_time.sleep = MagicMock()

        mock_get_runs.return_value = [
            {"databaseId": 1, "status": "in_progress"},
        ]
        mock_collect.return_value = ([], [1])

        allocs = [self._alloc(jobs=2)]
        result = wait_for_load_test(
            "branch",
            datetime.now(tz=UTC),
            allocs,
            timeout_minutes=1,
        )
        assert result.timed_out is True

    @patch("load_test_monitor._collect_job_results")
    @patch("load_test_monitor._get_filtered_runs")
    @patch("load_test_monitor.time")
    def test_no_runs_then_completed(self, mock_time, mock_get_runs, mock_collect):
        # Iteration 1: no runs, sleep. Iteration 2: completed.
        # time.time() calls: deadline(79), start(80), while#1(88), while#2(88), duration(104)
        mock_time.time.side_effect = [100.0, 100.0, 100.0, 100.0, 160.0]
        mock_time.sleep = MagicMock()

        mock_get_runs.side_effect = [
            [],  # first poll: no runs
            [{"databaseId": 1, "status": "completed"}],  # second poll
        ]
        mock_collect.return_value = (
            [JobResult("load-x (1)", "success", "x")],
            [1],
        )

        allocs = [self._alloc(jobs=1)]
        result = wait_for_load_test(
            "branch",
            datetime.now(tz=UTC),
            allocs,
            timeout_minutes=10,
        )
        assert result.timed_out is False
        assert result.completed_jobs == 1
        mock_time.sleep.assert_called()

    @patch("load_test_monitor._print_progress")
    @patch("load_test_monitor._collect_job_results")
    @patch("load_test_monitor._get_filtered_runs")
    @patch("load_test_monitor.time")
    def test_in_progress_sleeps(self, mock_time, mock_get_runs, mock_collect, mock_progress):
        # Iteration 1: in progress, sleep. Iteration 2: completed.
        # time.time() calls: deadline(79), start(80), while#1(88), while#2(88), duration(104)
        mock_time.time.side_effect = [100.0, 100.0, 100.0, 100.0, 160.0]
        mock_time.sleep = MagicMock()

        mock_get_runs.side_effect = [
            [{"databaseId": 1, "status": "in_progress"}],
            [{"databaseId": 1, "status": "completed"}],
        ]
        mock_collect.return_value = (
            [JobResult("load-x (1)", "success", "x")],
            [1],
        )

        allocs = [self._alloc(jobs=1)]
        result = wait_for_load_test(
            "branch",
            datetime.now(tz=UTC),
            allocs,
            timeout_minutes=10,
        )
        assert result.timed_out is False
        mock_progress.assert_called_once()

    @patch("load_test_monitor._collect_job_results")
    @patch("load_test_monitor._get_filtered_runs")
    @patch("load_test_monitor.time")
    def test_timeout_with_no_runs(self, mock_time, mock_get_runs, mock_collect):
        """Timeout when no runs were ever found returns empty results."""
        # time.time() calls: deadline(79), start(80), while-check(88) past deadline, duration(120)
        mock_time.time.side_effect = [100.0, 100.0, 99999.0, 99999.0]
        mock_time.sleep = MagicMock()
        mock_get_runs.return_value = []  # no runs at timeout

        allocs = [self._alloc(jobs=2)]
        result = wait_for_load_test(
            "branch",
            datetime.now(tz=UTC),
            allocs,
            timeout_minutes=1,
        )
        assert result.timed_out is True
        assert result.completed_jobs == 0
        assert result.jobs == []
        mock_collect.assert_not_called()
