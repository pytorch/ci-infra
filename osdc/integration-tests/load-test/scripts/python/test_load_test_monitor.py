"""Unit tests for load test monitoring and reporting."""

import json
from io import StringIO
from unittest.mock import MagicMock, patch

from distribution import RunnerAllocation
from load_test_monitor import (
    JobResult,
    LoadTestResults,
    _build_label_lookup,
    parse_runner_type,
    print_load_test_report,
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
            "test", "test-cluster", self._make_results(jobs, timed_out=True),
        )
        assert result is False

        captured = capsys.readouterr()
        assert "timed out" in captured.out

    def test_empty_results(self, capsys):
        result = print_load_test_report(
            "test", "test-cluster", self._make_results([]),
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
