#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pytest>=7.0"]
# ///
"""Unit tests for the log_rotator module."""

from __future__ import annotations

import argparse
import datetime
import io
import signal
from pathlib import Path
from unittest.mock import patch

from log_rotator import cleanup_old_logs, log_filename, main, parse_args, run

# ============================================================================
# parse_args
# ============================================================================


class TestParseArgs:
    def test_required_log_dir(self):
        args = parse_args(["--log-dir", "/tmp/logs"])
        assert args.log_dir == "/tmp/logs"

    def test_default_max_age_days(self):
        args = parse_args(["--log-dir", "/tmp/logs"])
        assert args.max_age_days == 30

    def test_custom_max_age_days(self):
        args = parse_args(["--log-dir", "/tmp/logs", "--max-age-days", "7"])
        assert args.max_age_days == 7

    def test_default_prefix(self):
        args = parse_args(["--log-dir", "/tmp/logs"])
        assert args.prefix == "access"

    def test_custom_prefix(self):
        args = parse_args(["--log-dir", "/tmp/logs", "--prefix", "server"])
        assert args.prefix == "server"


# ============================================================================
# log_filename
# ============================================================================


class TestLogFilename:
    def test_format(self):
        d = datetime.date(2025, 3, 15)
        assert log_filename("access", d) == "access.2025-03-15.log"

    def test_custom_prefix(self):
        d = datetime.date(2025, 1, 1)
        assert log_filename("server", d) == "server.2025-01-01.log"


# ============================================================================
# cleanup_old_logs
# ============================================================================


class TestCleanupOldLogs:
    def test_removes_old_files(self, tmp_path: Path):
        today = datetime.datetime.now(datetime.UTC).date()
        old_file = tmp_path / "access.2020-01-01.log"
        old_file.write_text("old data")
        recent_date = today - datetime.timedelta(days=5)
        recent_file = tmp_path / f"access.{recent_date.isoformat()}.log"
        recent_file.write_text("recent data")

        cleanup_old_logs(tmp_path, "access", max_age_days=30)

        assert not old_file.exists()
        assert recent_file.exists()

    def test_preserves_non_matching_files(self, tmp_path: Path):
        other_file = tmp_path / "something_else.txt"
        other_file.write_text("keep me")
        old_file = tmp_path / "access.2020-01-01.log"
        old_file.write_text("old")

        cleanup_old_logs(tmp_path, "access", max_age_days=30)

        assert other_file.exists()
        assert not old_file.exists()

    def test_ignores_nonexistent_dir(self):
        """Should not raise when directory doesn't exist."""
        cleanup_old_logs(Path("/nonexistent/path"), "access", max_age_days=30)

    def test_ignores_wrong_prefix(self, tmp_path: Path):
        old_file = tmp_path / "server.2020-01-01.log"
        old_file.write_text("old data")

        cleanup_old_logs(tmp_path, "access", max_age_days=30)

        assert old_file.exists()

    def test_ignores_malformed_dates(self, tmp_path: Path):
        bad_file = tmp_path / "access.not-a-date.log"
        bad_file.write_text("bad")

        cleanup_old_logs(tmp_path, "access", max_age_days=30)

        assert bad_file.exists()


# ============================================================================
# run — stdin echo to stdout
# ============================================================================


class TestRunStdoutEcho:
    def _make_args(self, log_dir: str) -> argparse.Namespace:
        return argparse.Namespace(log_dir=log_dir, max_age_days=30, prefix="access")

    def test_lines_echoed_to_stdout(self, tmp_path: Path):
        stdin = io.StringIO("line1\nline2\n")
        stdout = io.StringIO()
        args = self._make_args(str(tmp_path))

        run(args, stdin=stdin, stdout=stdout)

        assert stdout.getvalue() == "line1\nline2\n"

    def test_empty_lines_echoed(self, tmp_path: Path):
        stdin = io.StringIO("before\n\nafter\n")
        stdout = io.StringIO()
        args = self._make_args(str(tmp_path))

        run(args, stdin=stdin, stdout=stdout)

        assert stdout.getvalue() == "before\n\nafter\n"


# ============================================================================
# run — log file creation and content
# ============================================================================


class TestRunLogFile:
    def _make_args(self, log_dir: str, prefix: str = "access") -> argparse.Namespace:
        return argparse.Namespace(log_dir=log_dir, max_age_days=30, prefix=prefix)

    def test_creates_date_stamped_file(self, tmp_path: Path):
        stdin = io.StringIO("hello\n")
        stdout = io.StringIO()
        fixed_dt = datetime.datetime(2025, 3, 15, 12, 0, 0, tzinfo=datetime.UTC)
        args = self._make_args(str(tmp_path))

        run(args, stdin=stdin, stdout=stdout, now_fn=lambda: fixed_dt)

        expected_file = tmp_path / "access.2025-03-15.log"
        assert expected_file.exists()
        assert expected_file.read_text() == "hello\n"

    def test_content_written_to_file(self, tmp_path: Path):
        stdin = io.StringIO("line1\nline2\nline3\n")
        stdout = io.StringIO()
        fixed_dt = datetime.datetime(2025, 6, 1, 8, 0, 0, tzinfo=datetime.UTC)
        args = self._make_args(str(tmp_path))

        run(args, stdin=stdin, stdout=stdout, now_fn=lambda: fixed_dt)

        log_file = tmp_path / "access.2025-06-01.log"
        assert log_file.read_text() == "line1\nline2\nline3\n"

    def test_empty_lines_written_to_file(self, tmp_path: Path):
        stdin = io.StringIO("before\n\nafter\n")
        stdout = io.StringIO()
        fixed_dt = datetime.datetime(2025, 1, 1, 0, 0, 0, tzinfo=datetime.UTC)
        args = self._make_args(str(tmp_path))

        run(args, stdin=stdin, stdout=stdout, now_fn=lambda: fixed_dt)

        log_file = tmp_path / "access.2025-01-01.log"
        assert log_file.read_text() == "before\n\nafter\n"

    def test_custom_prefix(self, tmp_path: Path):
        stdin = io.StringIO("data\n")
        stdout = io.StringIO()
        fixed_dt = datetime.datetime(2025, 7, 4, 0, 0, 0, tzinfo=datetime.UTC)
        args = self._make_args(str(tmp_path), prefix="server")

        run(args, stdin=stdin, stdout=stdout, now_fn=lambda: fixed_dt)

        assert (tmp_path / "server.2025-07-04.log").exists()
        assert not (tmp_path / "access.2025-07-04.log").exists()

    def test_creates_log_dir_if_missing(self, tmp_path: Path):
        nested = tmp_path / "sub" / "dir" / "logs"
        stdin = io.StringIO("test\n")
        stdout = io.StringIO()
        fixed_dt = datetime.datetime(2025, 1, 1, 0, 0, 0, tzinfo=datetime.UTC)
        args = self._make_args(str(nested))

        run(args, stdin=stdin, stdout=stdout, now_fn=lambda: fixed_dt)

        assert nested.is_dir()
        assert (nested / "access.2025-01-01.log").exists()


# ============================================================================
# run — date rollover
# ============================================================================


class TestRunDateRollover:
    def test_date_change_creates_new_file(self, tmp_path: Path):
        """When UTC date changes mid-stream, a new log file is opened."""
        today = datetime.datetime.now(datetime.UTC).date()
        yesterday = today - datetime.timedelta(days=1)
        day1 = datetime.datetime.combine(yesterday, datetime.time(23, 59, 0), tzinfo=datetime.UTC)
        day2 = datetime.datetime.combine(today, datetime.time(0, 0, 1), tzinfo=datetime.UTC)
        # Call sequence: startup, line1, line2, line3
        dates = iter([day1, day1, day1, day2])

        stdin = io.StringIO("day1_line1\nday1_line2\nday2_line1\n")
        stdout = io.StringIO()
        args = argparse.Namespace(log_dir=str(tmp_path), max_age_days=30, prefix="access")

        run(args, stdin=stdin, stdout=stdout, now_fn=lambda: next(dates))

        file_day1 = tmp_path / f"access.{yesterday.isoformat()}.log"
        file_day2 = tmp_path / f"access.{today.isoformat()}.log"
        assert file_day1.exists()
        assert file_day2.exists()
        assert file_day1.read_text() == "day1_line1\nday1_line2\n"
        assert file_day2.read_text() == "day2_line1\n"


# ============================================================================
# run — old file cleanup
# ============================================================================


class TestRunCleanup:
    def test_startup_cleanup(self, tmp_path: Path):
        """Old files are removed on startup."""
        old_file = tmp_path / "access.2020-01-01.log"
        old_file.write_text("ancient")

        stdin = io.StringIO("")
        stdout = io.StringIO()
        fixed_dt = datetime.datetime(2025, 3, 15, 0, 0, 0, tzinfo=datetime.UTC)
        args = argparse.Namespace(log_dir=str(tmp_path), max_age_days=30, prefix="access")

        run(args, stdin=stdin, stdout=stdout, now_fn=lambda: fixed_dt)

        assert not old_file.exists()


# ============================================================================
# run — SIGTERM graceful shutdown
# ============================================================================


class TestRunSigterm:
    def test_sigterm_stops_processing(self, tmp_path: Path):
        """SIGTERM triggers graceful shutdown — file is flushed and closed."""
        lines_yielded = []

        class MockStdin:
            """Stdin that sends SIGTERM after yielding the second line.

            Flow: for-loop calls __next__ -> yields line -> body runs (write+flush)
            -> __next__ called again. We fire SIGTERM after the 2nd yield so that
            the shutdown flag is set before the 3rd line body runs.
            """

            def __init__(self):
                self.lines = iter(["line1\n", "line2\n", "line3\n", "line4\n", "line5\n"])

            def __iter__(self):
                return self

            def __next__(self):
                line = next(self.lines)
                lines_yielded.append(line)
                if len(lines_yielded) == 2:
                    # Fire SIGTERM after 2nd line is yielded.
                    # The for-loop body processes line2, then checks shutdown
                    # at the top of the next iteration before processing line3.
                    handler = signal.getsignal(signal.SIGTERM)
                    if callable(handler):
                        handler(signal.SIGTERM, None)
                return line

        stdin = MockStdin()
        stdout = io.StringIO()
        fixed_dt = datetime.datetime(2025, 1, 1, 0, 0, 0, tzinfo=datetime.UTC)
        args = argparse.Namespace(log_dir=str(tmp_path), max_age_days=30, prefix="access")

        run(args, stdin=stdin, stdout=stdout, now_fn=lambda: fixed_dt)

        log_file = tmp_path / "access.2025-01-01.log"
        assert log_file.exists()
        content = log_file.read_text()
        # Line 1 was fully processed before SIGTERM
        assert "line1\n" in content
        # Line 2 was yielded by __next__ (which triggered SIGTERM), but the
        # shutdown check at the top of the for-body breaks before writing it
        assert "line2" not in content
        # Not all 5 lines were consumed
        assert len(lines_yielded) < 5


# ============================================================================
# cleanup_old_logs — directory skip (line 45)
# ============================================================================


class TestCleanupSkipsDirectories:
    def test_skips_subdirectories(self, tmp_path: Path):
        """Subdirectories matching the log pattern are skipped, not deleted."""
        # Create a subdirectory that looks like a log file name
        subdir = tmp_path / "access.2020-01-01.log"
        subdir.mkdir()

        # Create a real old log file to prove cleanup still works
        old_file = tmp_path / "access.2020-01-02.log"
        old_file.write_text("old data")

        cleanup_old_logs(tmp_path, "access", max_age_days=30)

        # Directory is untouched (skipped by is_file check)
        assert subdir.is_dir()
        # Real file was cleaned up
        assert not old_file.exists()


# ============================================================================
# run — default stdin/stdout (lines 62, 64)
# ============================================================================


class TestRunDefaultStdinStdout:
    def test_defaults_to_sys_stdin_stdout(self, tmp_path: Path):
        """When stdin/stdout are not passed, run() uses sys.stdin/sys.stdout."""
        fake_stdin = io.StringIO("test line\n")
        fake_stdout = io.StringIO()
        fixed_dt = datetime.datetime(2025, 5, 1, 0, 0, 0, tzinfo=datetime.UTC)
        args = argparse.Namespace(log_dir=str(tmp_path), max_age_days=30, prefix="access")

        with patch("log_rotator.sys.stdin", fake_stdin), patch("log_rotator.sys.stdout", fake_stdout):
            run(args, now_fn=lambda: fixed_dt)

        # Verify output went through the patched sys.stdout
        assert fake_stdout.getvalue() == "test line\n"
        # Verify file was written
        log_file = tmp_path / "access.2025-05-01.log"
        assert log_file.exists()
        assert log_file.read_text() == "test line\n"


# ============================================================================
# main() — entry point (lines 112-113)
# ============================================================================


class TestMainEntrypoint:
    def test_main_calls_parse_args_and_run(self, tmp_path: Path):
        """main() parses args from sys.argv and calls run()."""
        log_dir = str(tmp_path / "logs")
        fake_stdin = io.StringIO("hello from main\n")
        fake_stdout = io.StringIO()

        with (
            patch("sys.argv", ["log_rotator.py", "--log-dir", log_dir, "--max-age-days", "7"]),
            patch("log_rotator.sys.stdin", fake_stdin),
            patch("log_rotator.sys.stdout", fake_stdout),
        ):
            main()

        # Verify log directory was created and file was written
        log_dir_path = Path(log_dir)
        assert log_dir_path.is_dir()
        log_files = list(log_dir_path.glob("access.*.log"))
        assert len(log_files) == 1
        assert log_files[0].read_text() == "hello from main\n"
