"""Unit tests for wheel_syncer.py."""

from __future__ import annotations

import argparse
import os
import signal
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from wheel_syncer import list_wheels, main, parse_args, run, sync_slug


# ---------------------------------------------------------------------------
# TestListWheels
# ---------------------------------------------------------------------------
class TestListWheels:
    def test_lists_whl_files(self):
        """Returns only .whl files, ignores non-.whl objects."""
        s3 = MagicMock()
        s3.list_objects_v2.return_value = {
            "Contents": [
                {"Key": "cpu/numpy-1.0.whl", "Size": 1234},
                {"Key": "cpu/README.md", "Size": 100},
                {"Key": "cpu/torch-2.0.tar.gz", "Size": 5678},
                {"Key": "cpu/scipy-1.0.whl", "Size": 9999},
            ],
            "IsTruncated": False,
        }
        result = list_wheels("bucket", "cpu/", s3)
        assert len(result) == 2
        assert {"key": "cpu/numpy-1.0.whl", "size": 1234} in result
        assert {"key": "cpu/scipy-1.0.whl", "size": 9999} in result

    def test_handles_pagination(self):
        """Follows NextContinuationToken when IsTruncated=True."""
        s3 = MagicMock()
        s3.list_objects_v2.side_effect = [
            {
                "Contents": [{"Key": "cpu/a.whl", "Size": 100}],
                "IsTruncated": True,
                "NextContinuationToken": "token1",
            },
            {
                "Contents": [{"Key": "cpu/b.whl", "Size": 200}],
                "IsTruncated": False,
            },
        ]
        result = list_wheels("bucket", "cpu/", s3)
        assert len(result) == 2
        assert s3.list_objects_v2.call_count == 2
        # Second call should include ContinuationToken
        second_call_kwargs = s3.list_objects_v2.call_args_list[1][1]
        assert second_call_kwargs["ContinuationToken"] == "token1"

    def test_empty_prefix(self):
        """Returns empty list when Contents is absent from S3 response."""
        s3 = MagicMock()
        s3.list_objects_v2.return_value = {"IsTruncated": False}
        result = list_wheels("bucket", "cpu/", s3)
        assert result == []

    def test_filters_to_prefix(self):
        """Passes the given prefix to S3 list call."""
        s3 = MagicMock()
        s3.list_objects_v2.return_value = {
            "Contents": [{"Key": "cu121/torch.whl", "Size": 100}],
            "IsTruncated": False,
        }
        list_wheels("my-bucket", "cu121/", s3)
        call_kwargs = s3.list_objects_v2.call_args[1]
        assert call_kwargs["Bucket"] == "my-bucket"
        assert call_kwargs["Prefix"] == "cu121/"


# ---------------------------------------------------------------------------
# TestSyncSlug
# ---------------------------------------------------------------------------
class TestSyncSlug:
    def test_downloads_missing_wheels(self, tmp_path):
        """Wheel in S3 but not on disk -> download_file called, file on disk."""
        s3 = MagicMock()
        s3.list_objects_v2.return_value = {
            "Contents": [{"Key": "cpu/numpy-1.0.whl", "Size": 100}],
            "IsTruncated": False,
        }

        def fake_download(_bucket, _key, dest):
            Path(dest).write_text("wheel data")

        s3.download_file.side_effect = fake_download

        downloaded, skipped = sync_slug("bucket", "cpu", tmp_path, s3)
        assert downloaded == 1
        assert skipped == 0
        assert (tmp_path / "cpu" / "numpy-1.0.whl").exists()

    def test_skips_existing_wheels(self, tmp_path):
        """Wheel already on disk -> no download_file call."""
        slug_dir = tmp_path / "cpu"
        slug_dir.mkdir()
        (slug_dir / "numpy-1.0.whl").write_text("existing")

        s3 = MagicMock()
        s3.list_objects_v2.return_value = {
            "Contents": [{"Key": "cpu/numpy-1.0.whl", "Size": 100}],
            "IsTruncated": False,
        }

        downloaded, skipped = sync_slug("bucket", "cpu", tmp_path, s3)
        assert downloaded == 0
        assert skipped == 1
        s3.download_file.assert_not_called()

    def test_atomic_write(self, tmp_path):
        """Download goes to .tmp first, then renamed to .whl."""
        s3 = MagicMock()
        s3.list_objects_v2.return_value = {
            "Contents": [{"Key": "cpu/pkg-1.0.whl", "Size": 50}],
            "IsTruncated": False,
        }

        tmp_seen = []

        def fake_download(_bucket, _key, dest):
            # dest should be the .tmp path
            tmp_seen.append(dest)
            assert dest.endswith(".tmp")
            Path(dest).write_text("data")

        s3.download_file.side_effect = fake_download

        sync_slug("bucket", "cpu", tmp_path, s3)
        assert len(tmp_seen) == 1
        # .tmp should be gone (renamed to final)
        assert not Path(tmp_seen[0]).exists()
        assert (tmp_path / "cpu" / "pkg-1.0.whl").exists()

    def test_creates_slug_directory(self, tmp_path):
        """Creates slug subdirectory if it doesn't exist."""
        s3 = MagicMock()
        s3.list_objects_v2.return_value = {"IsTruncated": False}

        assert not (tmp_path / "cu121").exists()
        sync_slug("bucket", "cu121", tmp_path, s3)
        assert (tmp_path / "cu121").is_dir()

    def test_download_failure_skips_file(self, tmp_path):
        """download_file raises -> file skipped, tmp cleaned, no crash."""
        s3 = MagicMock()
        s3.list_objects_v2.return_value = {
            "Contents": [
                {"Key": "cpu/bad.whl", "Size": 50},
                {"Key": "cpu/good.whl", "Size": 60},
            ],
            "IsTruncated": False,
        }

        call_count = 0

        def selective_download(_bucket, _key, dest):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                Path(dest).write_text("partial")
                raise OSError("network error")
            Path(dest).write_text("data")

        s3.download_file.side_effect = selective_download

        downloaded, _skipped = sync_slug("bucket", "cpu", tmp_path, s3)
        assert downloaded == 1
        # The failed wheel should not exist as final or tmp
        assert not (tmp_path / "cpu" / "bad.whl").exists()
        assert not (tmp_path / "cpu" / "bad.whl.tmp").exists()
        assert (tmp_path / "cpu" / "good.whl").exists()

    def test_skips_empty_filename(self, tmp_path):
        """Key that equals the prefix exactly (filename='') is skipped."""
        s3 = MagicMock()
        s3.list_objects_v2.return_value = {
            "Contents": [{"Key": "cpu/", "Size": 0}],
            "IsTruncated": False,
        }

        downloaded, skipped = sync_slug("bucket", "cpu", tmp_path, s3)
        assert downloaded == 0
        assert skipped == 0
        s3.download_file.assert_not_called()

    def test_path_traversal_skipped(self, tmp_path):
        """Keys with ../ path traversal are skipped."""
        s3 = MagicMock()
        s3.list_objects_v2.return_value = {
            "Contents": [{"Key": "cpu/../../etc/evil.whl", "Size": 100}],
            "IsTruncated": False,
        }

        downloaded, skipped = sync_slug("bucket", "cpu", tmp_path, s3)
        assert downloaded == 0
        assert skipped == 0
        s3.download_file.assert_not_called()

    def test_s3_list_failure_raises(self, tmp_path):
        """list_objects_v2 raises -> propagates to caller."""
        s3 = MagicMock()
        s3.list_objects_v2.side_effect = Exception("access denied")

        with pytest.raises(Exception, match="access denied"):
            sync_slug("bucket", "cpu", tmp_path, s3)


# ---------------------------------------------------------------------------
# TestParseArgs
# ---------------------------------------------------------------------------
class TestParseArgs:
    def test_required_args(self):
        args = parse_args(
            [
                "--wheelhouse-dir",
                "/data/wheelhouse",
                "--bucket",
                "my-bucket",
                "--slugs",
                "cpu,cu121,cu124",
            ]
        )
        assert args.wheelhouse_dir == "/data/wheelhouse"
        assert args.bucket == "my-bucket"
        assert args.slugs == "cpu,cu121,cu124"

    def test_defaults(self):
        args = parse_args(
            [
                "--wheelhouse-dir",
                "/data",
                "--bucket",
                "b",
                "--slugs",
                "cpu",
            ]
        )
        assert args.interval == 60
        assert args.once is False

    def test_overrides(self):
        args = parse_args(
            [
                "--wheelhouse-dir",
                "/data",
                "--bucket",
                "b",
                "--slugs",
                "cpu",
                "--interval",
                "30",
            ]
        )
        assert args.interval == 30

    def test_once_flag(self):
        args = parse_args(
            [
                "--wheelhouse-dir",
                "/data",
                "--bucket",
                "b",
                "--slugs",
                "cpu",
                "--once",
            ]
        )
        assert args.once is True

    def test_missing_required_arg(self):
        with pytest.raises(SystemExit):
            parse_args(["--wheelhouse-dir", "/data"])


class TestRun:
    def _make_args(self, tmp_path, **overrides):
        defaults = {
            "once": True,
            "wheelhouse_dir": str(tmp_path),
            "bucket": "test-bucket",
            "slugs": "cpu,cu121",
            "interval": 60,
        }
        defaults.update(overrides)
        return argparse.Namespace(**defaults)

    def test_single_cycle_with_once(self, tmp_path):
        """End-to-end with --once, verifies sync is called for each slug."""
        s3 = MagicMock()
        s3.list_objects_v2.return_value = {"IsTruncated": False}

        args = self._make_args(tmp_path)
        run(args, s3)

        # Should have called list_objects_v2 once per slug (cpu, cu121)
        assert s3.list_objects_v2.call_count == 2

    def test_multiple_slugs(self, tmp_path):
        """Syncs all configured slugs."""
        s3 = MagicMock()
        s3.list_objects_v2.return_value = {"IsTruncated": False}

        args = self._make_args(tmp_path, slugs="cpu,cu121,cu124")
        run(args, s3)

        prefixes = [c[1]["Prefix"] for c in s3.list_objects_v2.call_args_list]
        assert "cpu/" in prefixes
        assert "cu121/" in prefixes
        assert "cu124/" in prefixes

    def test_liveness_probe(self, tmp_path):
        """'/tmp/last-success' is touched after each cycle."""
        last_success = Path("/tmp/last-success")
        last_success.unlink(missing_ok=True)

        s3 = MagicMock()
        s3.list_objects_v2.return_value = {"IsTruncated": False}

        args = self._make_args(tmp_path)
        run(args, s3)

        assert last_success.exists()

    def test_empty_bucket(self, tmp_path):
        """No wheels in S3, no errors, liveness still touched."""
        last_success = Path("/tmp/last-success")
        last_success.unlink(missing_ok=True)

        s3 = MagicMock()
        s3.list_objects_v2.return_value = {"IsTruncated": False}

        args = self._make_args(tmp_path, slugs="cpu")
        run(args, s3)

        s3.download_file.assert_not_called()
        assert last_success.exists()

    def test_slug_failure_continues(self, tmp_path):
        """One slug's list_objects_v2 raises, other slugs still synced."""
        s3 = MagicMock()
        call_count = 0

        def conditional_list(**kwargs):
            nonlocal call_count
            call_count += 1
            if kwargs.get("Prefix") == "cpu/":
                raise Exception("s3 error")
            return {"IsTruncated": False}

        s3.list_objects_v2.side_effect = conditional_list

        args = self._make_args(tmp_path, slugs="cpu,cu121")
        run(args, s3)

        # Both slugs were attempted
        assert call_count == 2

    def test_sigterm_handling(self, tmp_path):
        """Verify SIGTERM handler is installed."""
        s3 = MagicMock()
        s3.list_objects_v2.return_value = {"IsTruncated": False}

        args = self._make_args(tmp_path)
        run(args, s3)

        handler = signal.getsignal(signal.SIGTERM)
        assert handler is not signal.SIG_DFL

    def test_sigterm_stops_loop(self, tmp_path):
        """Send SIGTERM during a cycle, loop exits."""
        s3 = MagicMock()
        call_count = 0

        def list_with_sigterm(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                os.kill(os.getpid(), signal.SIGTERM)
            return {"IsTruncated": False}

        s3.list_objects_v2.side_effect = list_with_sigterm

        args = self._make_args(tmp_path, once=False, slugs="cpu", interval=1)
        with patch("wheel_syncer.time.sleep"):
            run(args, s3)

        # If we get here, SIGTERM stopped the loop
        assert call_count >= 1


class TestMain:
    def test_main_invokes_run(self):
        with (
            patch("wheel_syncer.parse_args") as mock_parse,
            patch("wheel_syncer.run") as mock_run,
            patch.dict("sys.modules", {"boto3": MagicMock()}),
        ):
            mock_args = MagicMock()
            mock_parse.return_value = mock_args
            boto3_mock = sys.modules["boto3"]
            main()
            mock_parse.assert_called_once()
            boto3_mock.client.assert_called_once_with("s3")
            mock_run.assert_called_once_with(mock_args, boto3_mock.client.return_value)
