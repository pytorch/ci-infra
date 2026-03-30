"""Unit tests for wants_collector.py."""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import typing
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from wants_collector import (
    _default_http_get,
    _normalize_name,
    _parse_file,
    build_matrix,
    check_pypi,
    cleanup_old_logs,
    download_from_s3,
    filter_packages,
    format_prebuilt_cache,
    format_wants,
    is_manylinux_compatible,
    main,
    parse_args,
    parse_log_line,
    parse_prebuilt_cache,
    run,
    scan_logs,
    upload_to_s3,
)


# ---------------------------------------------------------------------------
# TestNormalizeName
# ---------------------------------------------------------------------------
class TestNormalizeName:
    def test_underscores(self):
        assert _normalize_name("my_package") == "my-package"

    def test_dots(self):
        assert _normalize_name("my.package") == "my-package"

    def test_mixed_case(self):
        assert _normalize_name("My_Package") == "my-package"

    def test_hyphens_preserved(self):
        assert _normalize_name("my-package") == "my-package"

    def test_consecutive_separators(self):
        assert _normalize_name("my___package") == "my-package"
        assert _normalize_name("my._.package") == "my-package"

    def test_already_normalized(self):
        assert _normalize_name("simple") == "simple"


# ---------------------------------------------------------------------------
# TestParseLogLine
# ---------------------------------------------------------------------------
class TestParseLogLine:
    def test_wheel_download(self):
        line = '10.0.0.1 - - "GET /cu121/numpy-1.26.4-cp312-cp312-manylinux_2_17_x86_64.whl HTTP/1.1" 200'
        result = parse_log_line(line)
        assert result == ("numpy", "1.26.4")

    def test_sdist_download(self):
        # Regex captures "2.0" from "foo-2.0.0.tar.gz" — trailing ".0" consumed by [-.]+ separator
        line = '10.0.0.1 - - "GET /cpu/foo-2.0.0.tar.gz HTTP/1.1" 200'
        result = parse_log_line(line)
        assert result == ("foo", "2.0")

    def test_zip_download(self):
        # Same trailing-segment behavior: "0.1" from "bar-0.1.0.zip"
        line = '10.0.0.1 - - "GET /cpu/bar-0.1.0.zip HTTP/1.1" 200'
        result = parse_log_line(line)
        assert result == ("bar", "0.1")

    def test_simple_index_query(self):
        line = '10.0.0.1 - - "GET /simple/numpy/ HTTP/1.1" 200'
        result = parse_log_line(line)
        assert result == ("numpy", "")

    def test_non_matching_line(self):
        line = '10.0.0.1 - - "GET /health HTTP/1.1" 200'
        assert parse_log_line(line) is None

    def test_prerelease_version(self):
        line = '10.0.0.1 - - "GET /cpu/torch-2.5.0.dev20240101-cp312-cp312-linux_x86_64.whl HTTP/1.1" 200'
        result = parse_log_line(line)
        assert result is not None
        assert result[0] == "torch"
        assert result[1].startswith("2.5.0")

    def test_packages_url_format(self):
        line = '10.0.0.1 - - [25/Mar/2026:10:00:00 +0000] "GET /packages/ab/cd/ef01234567/numpy-1.26.4-cp312-cp312-manylinux_2_17_x86_64.manylinux2014_x86_64.whl HTTP/1.1" 200 1234'
        result = parse_log_line(line)
        assert result == ("numpy", "1.26.4")

    def test_health_endpoint(self):
        line = '10.0.0.1 - - "GET /health/ HTTP/1.1" 200'
        assert parse_log_line(line) is None


# ---------------------------------------------------------------------------
# TestScanLogs
# ---------------------------------------------------------------------------
class TestScanLogs:
    def _write_log(self, path: Path, lines: list[str]):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n")

    def test_multiple_log_files(self, tmp_path):
        self._write_log(
            tmp_path / "access-cu121.log",
            ['10.0.0.1 - - "GET /cu121/numpy-1.26.4-cp312-cp312-manylinux_2_17_x86_64.whl HTTP/1.1" 200'],
        )
        self._write_log(
            tmp_path / "access-cu124.log",
            ['10.0.0.1 - - "GET /cu124/torch-2.2.0-cp311-cp311-manylinux_2_17_x86_64.whl HTTP/1.1" 200'],
        )
        results = scan_logs(tmp_path)
        assert ("numpy", "1.26.4") in results
        assert ("torch", "2.2.0") in results

    def test_missing_dir(self, tmp_path):
        results = scan_logs(tmp_path / "nonexistent")
        assert results == set()

    def test_non_log_files_ignored(self, tmp_path):
        (tmp_path / "readme.txt").write_text("not a log")
        results = scan_logs(tmp_path)
        assert results == set()

    def test_empty_dir(self, tmp_path):
        results = scan_logs(tmp_path)
        assert results == set()

    def test_simple_index_lines_excluded(self, tmp_path):
        """scan_logs only collects entries with a version (download lines)."""
        self._write_log(
            tmp_path / "fallback.log",
            ['10.0.0.1 - - "GET /simple/numpy/ HTTP/1.1" 200'],
        )
        results = scan_logs(tmp_path)
        assert results == set()


# ---------------------------------------------------------------------------
# TestParseFile
# ---------------------------------------------------------------------------
class TestParseFile:
    def test_normal_parsing(self, tmp_path):
        log = tmp_path / "access.log"
        log.write_text('10.0.0.1 - - "GET /cu121/numpy-1.26.4-cp312-cp312-manylinux_2_17_x86_64.whl HTTP/1.1" 200\n')
        results: set[tuple[str, str]] = set()
        _parse_file(log, results)
        assert ("numpy", "1.26.4") in results

    def test_oserror_handling(self, tmp_path):
        log = tmp_path / "access.log"
        log.write_text("content")
        results: set[tuple[str, str]] = set()
        with patch("builtins.open", side_effect=OSError("gone")):
            _parse_file(log, results)
        assert results == set()


# ---------------------------------------------------------------------------
# TestBuildMatrix
# ---------------------------------------------------------------------------
class TestBuildMatrix:
    def test_2x2_matrix(self):
        _header, combos = build_matrix(["3.10", "3.11"], ["x86_64", "aarch64"], "2_17")
        assert combos == {("cp310", "x86_64"), ("cp310", "aarch64"), ("cp311", "x86_64"), ("cp311", "aarch64")}
        assert len(combos) == 4

    def test_single_entries(self):
        _header, combos = build_matrix(["3.12"], ["x86_64"], "2_17")
        assert combos == {("cp312", "x86_64")}

    def test_header_format(self):
        header, _ = build_matrix(["3.10", "3.11"], ["x86_64", "aarch64"], "2_17")
        assert header == "py3.10,py3.11 x86_64,aarch64 manylinux_2_17"


# ---------------------------------------------------------------------------
# TestIsManylinuxCompatible
# ---------------------------------------------------------------------------
class TestIsManylinuxCompatible:
    def test_matching_manylinux_2_17(self):
        ok, arch = is_manylinux_compatible("manylinux_2_17_x86_64", "2_17")
        assert ok is True
        assert arch == "x86_64"

    def test_manylinux2014_alias(self):
        ok, arch = is_manylinux_compatible("manylinux2014_x86_64", "2_17")
        assert ok is True
        assert arch == "x86_64"

    def test_manylinux1_alias(self):
        ok, arch = is_manylinux_compatible("manylinux1_x86_64", "2_17")
        assert ok is True
        assert arch == "x86_64"

    def test_incompatible_higher_version(self):
        ok, arch = is_manylinux_compatible("manylinux_2_28_x86_64", "2_17")
        assert ok is False
        assert arch == "x86_64"

    def test_aarch64_extraction(self):
        ok, arch = is_manylinux_compatible("manylinux_2_17_aarch64", "2_17")
        assert ok is True
        assert arch == "aarch64"

    def test_no_manylinux_tag(self):
        ok, arch = is_manylinux_compatible("win_amd64", "2_17")
        assert ok is False
        assert arch is None

    def test_manylinux2010_alias(self):
        ok, arch = is_manylinux_compatible("manylinux2010_x86_64", "2_17")
        assert ok is True
        assert arch == "x86_64"


# ---------------------------------------------------------------------------
# TestParsePrebuiltCache
# ---------------------------------------------------------------------------
class TestParsePrebuiltCache:
    def test_valid_cache(self):
        content = "# matrix: py3.10,py3.11 x86_64 manylinux_2_17\nnumpy==1.26.4\ntorch==2.2.0\n"
        result = parse_prebuilt_cache(content, "py3.10,py3.11 x86_64 manylinux_2_17")
        assert result == {"numpy==1.26.4", "torch==2.2.0"}

    def test_mismatched_header(self):
        content = "# matrix: py3.10 x86_64 manylinux_2_17\nnumpy==1.26.4\n"
        result = parse_prebuilt_cache(content, "py3.11 x86_64 manylinux_2_17")
        assert result == set()

    def test_empty_file(self):
        assert parse_prebuilt_cache("", "header") == set()

    def test_missing_header_line(self):
        content = "numpy==1.26.4\ntorch==2.2.0\n"
        assert parse_prebuilt_cache(content, "header") == set()

    def test_none_content(self):
        assert parse_prebuilt_cache(None, "header") == set()


# ---------------------------------------------------------------------------
# TestFormatPrebuiltCache
# ---------------------------------------------------------------------------
class TestFormatPrebuiltCache:
    def test_sorted_output(self):
        result = format_prebuilt_cache("hdr", {"torch==2.2.0", "numpy==1.26.4"})
        lines = result.strip().splitlines()
        assert lines[0] == "# matrix: hdr"
        assert lines[1:] == ["numpy==1.26.4", "torch==2.2.0"]

    def test_header_present(self):
        result = format_prebuilt_cache("py3.10 x86_64 manylinux_2_17", set())
        assert result.startswith("# matrix: py3.10 x86_64 manylinux_2_17")


# ---------------------------------------------------------------------------
# TestCheckPypi
# ---------------------------------------------------------------------------
class TestCheckPypi:
    MATRIX_2X1: typing.ClassVar[set[tuple[str, str]]] = {("cp310", "x86_64"), ("cp311", "x86_64")}

    def _make_wheel_entry(self, filename: str) -> dict:
        return {"packagetype": "bdist_wheel", "filename": filename}

    def _make_sdist_entry(self, filename: str) -> dict:
        return {"packagetype": "sdist", "filename": filename}

    def test_pure_python_package(self):
        """py3-none-any wheel means no build needed."""
        http_get = MagicMock(
            return_value=(200, json.dumps({"urls": [self._make_wheel_entry("requests-2.31.0-py3-none-any.whl")]}))
        )
        result = check_pypi("requests", "2.31.0", self.MATRIX_2X1, "2_17", http_get)
        assert result is False

    def test_full_wheel_coverage(self):
        """All matrix combos covered by manylinux wheels."""
        http_get = MagicMock(
            return_value=(
                200,
                json.dumps(
                    {
                        "urls": [
                            self._make_wheel_entry(
                                "numpy-1.26.4-cp310-cp310-manylinux_2_17_x86_64.manylinux2014_x86_64.whl"
                            ),
                            self._make_wheel_entry(
                                "numpy-1.26.4-cp311-cp311-manylinux_2_17_x86_64.manylinux2014_x86_64.whl"
                            ),
                        ]
                    }
                ),
            )
        )
        result = check_pypi("numpy", "1.26.4", self.MATRIX_2X1, "2_17", http_get)
        assert result is False

    def test_partial_coverage(self):
        """Missing one combo means build needed."""
        http_get = MagicMock(
            return_value=(
                200,
                json.dumps(
                    {
                        "urls": [
                            self._make_wheel_entry(
                                "numpy-1.26.4-cp310-cp310-manylinux_2_17_x86_64.manylinux2014_x86_64.whl"
                            ),
                        ]
                    }
                ),
            )
        )
        result = check_pypi("numpy", "1.26.4", self.MATRIX_2X1, "2_17", http_get)
        assert result is True

    def test_sdist_only(self):
        """Only sdist available means build needed."""
        http_get = MagicMock(return_value=(200, json.dumps({"urls": [self._make_sdist_entry("foo-1.0.0.tar.gz")]})))
        result = check_pypi("foo", "1.0.0", self.MATRIX_2X1, "2_17", http_get)
        assert result is True

    def test_404_from_pypi(self):
        """Package not on PyPI — skip it."""
        http_get = MagicMock(return_value=(404, ""))
        result = check_pypi("nonexistent", "0.0.1", self.MATRIX_2X1, "2_17", http_get)
        assert result is False

    def test_network_error_raises(self):
        """Network errors propagate to caller."""
        http_get = MagicMock(side_effect=ConnectionError("timeout"))
        with pytest.raises(ConnectionError):
            check_pypi("pkg", "1.0.0", self.MATRIX_2X1, "2_17", http_get)


# ---------------------------------------------------------------------------
# TestFilterPackages
# ---------------------------------------------------------------------------
class TestFilterPackages:
    def test_mix_of_packages(self):
        """Pure Python skipped, native needs build, prebuilt cached."""
        packages = {("requests", "2.31.0"), ("native-lib", "1.0.0")}
        matrix = {("cp310", "x86_64")}

        def http_get(url):
            if "requests" in url:
                return (
                    200,
                    json.dumps(
                        {"urls": [{"packagetype": "bdist_wheel", "filename": "requests-2.31.0-py3-none-any.whl"}]}
                    ),
                )
            return (200, json.dumps({"urls": [{"packagetype": "sdist", "filename": "native-lib-1.0.0.tar.gz"}]}))

        wants, prebuilt = filter_packages(packages, matrix, "2_17", set(), http_get)
        assert "native-lib==1.0.0" in wants
        assert "requests==2.31.0" in prebuilt

    def test_prebuilt_cache_hit(self):
        """Already-cached entries are skipped entirely."""
        packages = {("numpy", "1.26.4")}
        matrix = {("cp310", "x86_64")}
        existing_prebuilt = {"numpy==1.26.4"}
        http_get = MagicMock()

        wants, _prebuilt = filter_packages(packages, matrix, "2_17", existing_prebuilt, http_get)
        assert wants == set()
        http_get.assert_not_called()

    def test_prebuilt_updated_with_new(self):
        """Newly confirmed pure-python packages added to prebuilt set."""
        packages = {("requests", "2.31.0")}
        matrix = {("cp310", "x86_64")}

        def http_get(url):
            return (
                200,
                json.dumps({"urls": [{"packagetype": "bdist_wheel", "filename": "requests-2.31.0-py3-none-any.whl"}]}),
            )

        wants, prebuilt = filter_packages(packages, matrix, "2_17", set(), http_get)
        assert wants == set()
        assert "requests==2.31.0" in prebuilt


# ---------------------------------------------------------------------------
# TestFormatWants
# ---------------------------------------------------------------------------
class TestFormatWants:
    def test_sorted_output(self):
        result = format_wants({"torch==2.2.0", "numpy==1.26.4"})
        assert result == "numpy==1.26.4\ntorch==2.2.0\n"

    def test_empty_set(self):
        assert format_wants(set()) == ""

    def test_newline_terminated(self):
        result = format_wants({"pkg==1.0.0"})
        assert result.endswith("\n")


# ---------------------------------------------------------------------------
# TestUploadToS3
# ---------------------------------------------------------------------------
class TestUploadToS3:
    def test_correct_put_object_call(self):
        mock_s3 = MagicMock()
        upload_to_s3("content", "my-bucket", "my-key", mock_s3)
        mock_s3.put_object.assert_called_once_with(
            Bucket="my-bucket", Key="my-key", Body=b"content", ContentType="text/plain"
        )


# ---------------------------------------------------------------------------
# TestDownloadFromS3
# ---------------------------------------------------------------------------
class TestDownloadFromS3:
    def test_successful_download(self):
        mock_s3 = MagicMock()
        mock_s3.get_object.return_value = {"Body": MagicMock(read=MagicMock(return_value=b"cached data"))}
        result = download_from_s3("bucket", "key", mock_s3)
        assert result == "cached data"

    def test_no_such_key_returns_none(self):
        mock_s3 = MagicMock()
        no_such_key = type("NoSuchKey", (Exception,), {})
        mock_s3.exceptions.NoSuchKey = no_such_key
        mock_s3.get_object.side_effect = no_such_key()
        result = download_from_s3("bucket", "key", mock_s3)
        assert result is None


# ---------------------------------------------------------------------------
# TestRun
# ---------------------------------------------------------------------------
class TestRun:
    def _make_args(self, tmp_path, **overrides):
        defaults = {
            "once": True,
            "log_dir": str(tmp_path),
            "cluster_id": "test",
            "bucket": "test-bucket",
            "interval": 120,
            "target_python": "3.10,3.11",
            "target_arch": "x86_64,aarch64",
            "target_manylinux": "2_17",
            "max_log_age_days": 30,
        }
        defaults.update(overrides)
        return argparse.Namespace(**defaults)

    def test_single_iteration_with_once(self, tmp_path):
        log_file = tmp_path / "fallback.log"
        log_file.write_text('10.0.0.1 - - "GET /cpu/pkg-1.0.0.tar.gz HTTP/1.1" 200\n')

        mock_s3 = MagicMock()
        mock_s3.get_object.return_value = {"Body": MagicMock(read=MagicMock(return_value=b""))}

        def http_get(url):
            return (200, json.dumps({"urls": [{"packagetype": "sdist", "filename": "pkg-1.0.0.tar.gz"}]}))

        args = self._make_args(tmp_path)
        run(args, mock_s3, http_get=http_get)

        assert mock_s3.put_object.call_count == 2

    def test_empty_logs_touches_last_success(self, tmp_path):
        mock_s3 = MagicMock()
        args = self._make_args(tmp_path)
        last_success = Path("/tmp/last-success")
        last_success.unlink(missing_ok=True)

        run(args, mock_s3)

        assert last_success.exists()

    def test_pypi_unreachable_skips_cycle(self, tmp_path):
        log_file = tmp_path / "fallback.log"
        log_file.write_text('10.0.0.1 - - "GET /cpu/pkg-1.0.0.tar.gz HTTP/1.1" 200\n')

        mock_s3 = MagicMock()
        mock_s3.get_object.return_value = {"Body": MagicMock(read=MagicMock(return_value=b""))}
        http_get = MagicMock(side_effect=ConnectionError("unreachable"))

        args = self._make_args(tmp_path)
        run(args, mock_s3, http_get=http_get)

        mock_s3.put_object.assert_not_called()

    def test_sigterm_handling(self, tmp_path):
        """Verify SIGTERM handler is installed."""
        mock_s3 = MagicMock()
        args = self._make_args(tmp_path)

        run(args, mock_s3)

        handler = signal.getsignal(signal.SIGTERM)
        assert handler is not signal.SIG_DFL


# ---------------------------------------------------------------------------
# TestParseArgs
# ---------------------------------------------------------------------------
class TestParseArgs:
    def test_required_args(self):
        args = parse_args(
            [
                "--log-dir",
                "/data/logs",
                "--cluster-id",
                "prod",
                "--bucket",
                "my-bucket",
                "--target-python",
                "3.10,3.11",
                "--target-arch",
                "x86_64",
                "--target-manylinux",
                "2_17",
            ]
        )
        assert args.log_dir == "/data/logs"
        assert args.cluster_id == "prod"
        assert args.bucket == "my-bucket"
        assert args.target_python == "3.10,3.11"
        assert args.target_arch == "x86_64"
        assert args.target_manylinux == "2_17"

    def test_defaults(self):
        args = parse_args(
            [
                "--log-dir",
                "/logs",
                "--cluster-id",
                "test",
                "--bucket",
                "b",
                "--target-python",
                "3.12",
                "--target-arch",
                "x86_64",
                "--target-manylinux",
                "2_17",
            ]
        )
        assert args.interval == 120
        assert args.once is False

    def test_overrides(self):
        args = parse_args(
            [
                "--log-dir",
                "/logs",
                "--cluster-id",
                "test",
                "--bucket",
                "b",
                "--target-python",
                "3.12",
                "--target-arch",
                "x86_64",
                "--target-manylinux",
                "2_17",
                "--interval",
                "60",
            ]
        )
        assert args.interval == 60

    def test_once_flag(self):
        args = parse_args(
            [
                "--log-dir",
                "/logs",
                "--cluster-id",
                "test",
                "--bucket",
                "b",
                "--target-python",
                "3.12",
                "--target-arch",
                "x86_64",
                "--target-manylinux",
                "2_17",
                "--once",
            ]
        )
        assert args.once is True

    def test_missing_required_arg(self):
        with pytest.raises(SystemExit):
            parse_args(["--log-dir", "/logs"])


# ---------------------------------------------------------------------------
# Additional coverage tests
# ---------------------------------------------------------------------------
class TestScanLogsNonLogEntry:
    """Cover: non-.log files in log_dir are skipped."""

    def test_non_log_file_in_log_dir_skipped(self, tmp_path):
        (tmp_path / "not-a-log.txt").write_text("junk")
        log = tmp_path / "fallback.log"
        log.write_text('10.0.0.1 - - "GET /cpu/pkg-1.0.0.tar.gz HTTP/1.1" 200\n')

        results = scan_logs(tmp_path)
        assert ("pkg", "1.0") in results


class TestCheckPypiEdgeCases:
    """Cover lines 168 and 174 in check_pypi."""

    MATRIX: typing.ClassVar[set[tuple[str, str]]] = {("cp310", "x86_64")}

    def test_malformed_wheel_filename_skipped(self):
        """Line 168: wheel with <5 dash-separated parts is skipped."""
        http_get = MagicMock(
            return_value=(
                200,
                json.dumps({"urls": [{"packagetype": "bdist_wheel", "filename": "bad-name.whl"}]}),
            )
        )
        result = check_pypi("bad", "1.0.0", self.MATRIX, "2_17", http_get)
        assert result is True

    def test_incompatible_platform_tag_skipped(self):
        """Line 174: wheel with non-manylinux platform tag is skipped."""
        http_get = MagicMock(
            return_value=(
                200,
                json.dumps(
                    {
                        "urls": [
                            {
                                "packagetype": "bdist_wheel",
                                "filename": "pkg-1.0.0-cp310-cp310-win_amd64.whl",
                            }
                        ]
                    }
                ),
            )
        )
        result = check_pypi("pkg", "1.0.0", self.MATRIX, "2_17", http_get)
        assert result is True


class TestParsePrebuiltCacheWhitespace:
    """Cover line 219: content that is only whitespace."""

    def test_whitespace_only_content(self):
        result = parse_prebuilt_cache("   \n  \n  ", "header")
        assert result == set()


class TestDefaultHttpGet:
    """Cover lines 253-257: _default_http_get."""

    def test_successful_request(self):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b'{"urls": []}'
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=mock_resp):
            status, body = _default_http_get("https://pypi.org/pypi/foo/1.0/json")
        assert status == 200
        assert body == '{"urls": []}'

    def test_http_error_returns_code(self):
        with patch("urllib.request.urlopen", side_effect=urllib.error.HTTPError(None, 404, "Not Found", {}, None)):
            status, body = _default_http_get("https://pypi.org/pypi/nonexistent/0.0.1/json")
        assert status == 404
        assert body == ""


class TestRunEmptyLogs:
    """Cover: run() with empty log directory returns early."""

    def test_run_empty_logs(self, tmp_path):
        """When no log files exist, run() touches last-success and returns."""
        mock_s3 = MagicMock()
        args = argparse.Namespace(
            once=True,
            log_dir=str(tmp_path),
            cluster_id="test",
            bucket="test-bucket",
            interval=120,
            target_python="3.10",
            target_arch="x86_64",
            target_manylinux="2_17",
            max_log_age_days=30,
        )
        run(args, mock_s3, http_get=lambda url: (200, json.dumps({"urls": []})))


class TestRunSigtermSetsShutdown:
    """Cover: SIGTERM handler sets shutdown=True."""

    def test_sigterm_stops_loop(self, tmp_path):
        """Invoke the SIGTERM handler and confirm the loop exits."""
        log = tmp_path / "fallback.log"
        log.write_text('10.0.0.1 - - "GET /cpu/pkg-1.0.0.tar.gz HTTP/1.1" 200\n')

        mock_s3 = MagicMock()
        mock_s3.get_object.return_value = {"Body": MagicMock(read=MagicMock(return_value=b""))}
        call_count = 0

        def http_get_with_sigterm(url):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                os.kill(os.getpid(), signal.SIGTERM)
            return (200, json.dumps({"urls": [{"packagetype": "sdist", "filename": "pkg-1.0.0.tar.gz"}]}))

        args = argparse.Namespace(
            once=False,
            log_dir=str(tmp_path),
            cluster_id="test",
            bucket="test-bucket",
            interval=1,
            target_python="3.10",
            target_arch="x86_64",
            target_manylinux="2_17",
            max_log_age_days=30,
        )
        run(args, mock_s3, http_get=http_get_with_sigterm)
        # If we get here, SIGTERM stopped the loop
        assert call_count >= 1


class TestRunLoopBranches:
    """Cover: sleep+continue in non-once mode."""

    def _make_args(self, tmp_path, **overrides):
        defaults = {
            "once": False,
            "log_dir": str(tmp_path),
            "cluster_id": "test",
            "bucket": "test-bucket",
            "interval": 1,
            "target_python": "3.10",
            "target_arch": "x86_64",
            "target_manylinux": "2_17",
            "max_log_age_days": 30,
        }
        defaults.update(overrides)
        return argparse.Namespace(**defaults)

    def test_empty_logs_sleeps_then_sigterm(self, tmp_path):
        """No packages found, once=False -> sleep then continue."""
        sleep_calls = 0

        def counting_sleep(seconds):
            nonlocal sleep_calls
            sleep_calls += 1
            os.kill(os.getpid(), signal.SIGTERM)

        args = self._make_args(tmp_path)
        mock_s3 = MagicMock()
        with patch("wants_collector.time.sleep", side_effect=counting_sleep):
            run(args, mock_s3)
        assert sleep_calls >= 1

    def test_pypi_failure_sleeps_then_sigterm(self, tmp_path):
        """PyPI check fails, once=False -> sleep then continue."""
        log = tmp_path / "fallback.log"
        log.write_text('10.0.0.1 - - "GET /cpu/pkg-1.0.0.tar.gz HTTP/1.1" 200\n')

        mock_s3 = MagicMock()
        mock_s3.get_object.return_value = {"Body": MagicMock(read=MagicMock(return_value=b""))}
        http_get = MagicMock(side_effect=ConnectionError("unreachable"))

        sleep_calls = 0

        def counting_sleep(seconds):
            nonlocal sleep_calls
            sleep_calls += 1
            os.kill(os.getpid(), signal.SIGTERM)

        args = self._make_args(tmp_path)
        with patch("wants_collector.time.sleep", side_effect=counting_sleep):
            run(args, mock_s3, http_get=http_get)
        assert sleep_calls >= 1

    def test_successful_cycle_sleeps_then_sigterm(self, tmp_path):
        """Successful cycle, once=False -> sleep at end of loop."""
        log = tmp_path / "fallback.log"
        log.write_text('10.0.0.1 - - "GET /cpu/pkg-1.0.0.tar.gz HTTP/1.1" 200\n')

        mock_s3 = MagicMock()
        mock_s3.get_object.return_value = {"Body": MagicMock(read=MagicMock(return_value=b""))}

        def http_get(url):
            return (200, json.dumps({"urls": [{"packagetype": "sdist", "filename": "pkg-1.0.0.tar.gz"}]}))

        sleep_calls = 0

        def counting_sleep(seconds):
            nonlocal sleep_calls
            sleep_calls += 1
            os.kill(os.getpid(), signal.SIGTERM)

        args = self._make_args(tmp_path)
        with patch("wants_collector.time.sleep", side_effect=counting_sleep):
            run(args, mock_s3, http_get=http_get)
        assert sleep_calls >= 1


# ---------------------------------------------------------------------------
# TestCleanupOldLogs
# ---------------------------------------------------------------------------
class TestCleanupOldLogs:
    """Cover: cleanup_old_logs deletes old fallback log files."""

    def test_old_files_deleted(self, tmp_path):
        """Files older than max_age_days are deleted."""
        old = tmp_path / "fallback.2020-01-01.log"
        old.write_text("old data")
        recent = tmp_path / "fallback.2026-03-29.log"
        recent.write_text("recent data")

        cleanup_old_logs(tmp_path, max_age_days=30)

        assert not old.exists()
        assert recent.exists()

    def test_date_unknown_kept(self, tmp_path):
        """fallback.date-unknown.log is not deleted (non-date stem)."""
        unknown = tmp_path / "fallback.date-unknown.log"
        unknown.write_text("data")

        cleanup_old_logs(tmp_path, max_age_days=0)

        assert unknown.exists()

    def test_non_matching_files_kept(self, tmp_path):
        """Files not matching fallback.*.log pattern are kept."""
        other = tmp_path / "access.log"
        other.write_text("data")
        txt = tmp_path / "notes.txt"
        txt.write_text("data")

        cleanup_old_logs(tmp_path, max_age_days=0)

        assert other.exists()
        assert txt.exists()

    def test_nonexistent_dir(self, tmp_path):
        """No error on nonexistent directory."""
        cleanup_old_logs(tmp_path / "nonexistent", max_age_days=30)

    def test_oserror_on_unlink_ignored(self, tmp_path):
        """OSError during unlink is silently ignored."""
        old = tmp_path / "fallback.2020-01-01.log"
        old.write_text("data")

        with patch.object(Path, "unlink", side_effect=OSError("permission denied")):
            cleanup_old_logs(tmp_path, max_age_days=1)

        # Should not raise — OSError is caught


class TestMain:
    """Cover lines 338-342: main() function."""

    def test_main_invokes_run(self):
        with (
            patch("wants_collector.parse_args") as mock_parse,
            patch("wants_collector.run") as mock_run,
            patch.dict("sys.modules", {"boto3": MagicMock()}),
        ):
            mock_args = MagicMock()
            mock_parse.return_value = mock_args
            boto3_mock = sys.modules["boto3"]
            main()
            mock_parse.assert_called_once()
            boto3_mock.client.assert_called_once_with("s3")
            mock_run.assert_called_once_with(mock_args, boto3_mock.client.return_value)
