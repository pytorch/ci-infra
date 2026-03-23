"""Unit tests for smoke test helper functions.

Tests the remote verification helpers (assert_metric_fresh_in_mimir,
assert_logs_fresh_in_loki) with mocked HTTP responses.
"""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest
from helpers import (
    REMOTE_RETRIES,
    assert_logs_fresh_in_loki,
    assert_metric_fresh_in_mimir,
)

# ============================================================================
# assert_metric_fresh_in_mimir
# ============================================================================


class TestAssertMetricFreshInMimir:
    """Tests for assert_metric_fresh_in_mimir helper."""

    def test_success_fresh_data(self) -> None:
        """Fresh metric data should pass without error."""
        now = time.time()
        mock_result = {
            "status": "success",
            "data": {"result": [{"value": [now - 30, "1"]}]},
        }
        with patch("helpers.query_mimir", return_value=mock_result):
            assert_metric_fresh_in_mimir(
                "http://mimir/api/v1/query", "up{}", "user", "pass", description="test"
            )

    def test_stale_data_retries_then_raises(self) -> None:
        """Persistently stale data should retry then raise AssertionError."""
        now = time.time()
        mock_result = {
            "status": "success",
            "data": {"result": [{"value": [now - 900, "1"]}]},
        }
        with (
            patch("helpers.query_mimir", return_value=mock_result) as mock_query,
            patch("helpers.time.sleep"),
            pytest.raises(AssertionError, match="stale"),
        ):
            assert_metric_fresh_in_mimir(
                "http://mimir/api/v1/query",
                "up{}",
                "user",
                "pass",
                max_staleness=600,
                description="test",
            )
        assert mock_query.call_count == REMOTE_RETRIES

    def test_stale_then_fresh_recovers(self) -> None:
        """Stale data on first attempt, fresh on second — should succeed."""
        now = time.time()
        stale_result = {
            "status": "success",
            "data": {"result": [{"value": [now - 900, "1"]}]},
        }
        fresh_result = {
            "status": "success",
            "data": {"result": [{"value": [now - 30, "1"]}]},
        }

        call_count = 0

        def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return stale_result if call_count == 1 else fresh_result

        with patch("helpers.query_mimir", side_effect=side_effect), patch("helpers.time.sleep"):
            assert_metric_fresh_in_mimir(
                "http://mimir/api/v1/query", "up{}", "user", "pass", description="test"
            )
        assert call_count == 2

    def test_no_data_retries_then_raises(self) -> None:
        """Persistently empty results should retry then raise AssertionError."""
        mock_result = {"status": "success", "data": {"result": []}}
        with (
            patch("helpers.query_mimir", return_value=mock_result) as mock_query,
            patch("helpers.time.sleep"),
            pytest.raises(AssertionError, match="No metric series found"),
        ):
            assert_metric_fresh_in_mimir(
                "http://mimir/api/v1/query", "up{}", "user", "pass", description="test"
            )
        assert mock_query.call_count == REMOTE_RETRIES

    def test_no_data_then_fresh_recovers(self) -> None:
        """Empty results on first attempt, fresh data on second — should succeed."""
        now = time.time()
        empty_result = {"status": "success", "data": {"result": []}}
        fresh_result = {
            "status": "success",
            "data": {"result": [{"value": [now - 30, "1"]}]},
        }

        call_count = 0

        def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return empty_result if call_count == 1 else fresh_result

        with patch("helpers.query_mimir", side_effect=side_effect), patch("helpers.time.sleep"):
            assert_metric_fresh_in_mimir(
                "http://mimir/api/v1/query", "up{}", "user", "pass", description="test"
            )
        assert call_count == 2

    def test_network_error_retries_then_skips(self) -> None:
        """All retries failing with network error should pytest.skip."""
        with patch("helpers.query_mimir", return_value=None) as mock_query:
            mock_query.last_error = "Connection refused"
            with patch("helpers.time.sleep"), pytest.raises(pytest.skip.Exception):
                assert_metric_fresh_in_mimir(
                    "http://mimir/api/v1/query", "up{}", "user", "pass", description="test"
                )
            assert mock_query.call_count == REMOTE_RETRIES

    def test_retry_succeeds_on_second_attempt(self) -> None:
        """Should succeed if second attempt returns fresh data."""
        now = time.time()
        fresh_result = {
            "status": "success",
            "data": {"result": [{"value": [now - 30, "1"]}]},
        }

        call_count = 0

        def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return None
            return fresh_result

        with patch("helpers.query_mimir", side_effect=side_effect) as mock_query:
            mock_query.last_error = "timeout"
            with patch("helpers.time.sleep"):
                assert_metric_fresh_in_mimir(
                    "http://mimir/api/v1/query", "up{}", "user", "pass", description="test"
                )
        assert call_count == 2

    def test_bad_status_raises(self) -> None:
        """Non-success status from Mimir should raise AssertionError."""
        mock_result = {"status": "error", "data": {"result": []}}
        with patch("helpers.query_mimir", return_value=mock_result), pytest.raises(AssertionError, match="status 'error'"):
            assert_metric_fresh_in_mimir(
                "http://mimir/api/v1/query", "up{}", "user", "pass", description="test"
            )


# ============================================================================
# assert_logs_fresh_in_loki
# ============================================================================


class TestAssertLogsFreshInLoki:
    """Tests for assert_logs_fresh_in_loki helper."""

    def test_success_fresh_data(self) -> None:
        """Fresh log data should pass without error."""
        now_ns = str(int(time.time() * 1e9))
        mock_result = {
            "status": "success",
            "data": {"result": [{"values": [[now_ns, "log line"]]}]},
        }
        with patch("helpers.query_loki", return_value=mock_result):
            assert_logs_fresh_in_loki(
                "http://loki/loki/api/v1/query_range", '{cluster="test"}', "user", "pass", description="test"
            )

    def test_stale_data_retries_then_raises(self) -> None:
        """Persistently stale log data should retry then raise AssertionError."""
        old_ns = str(int((time.time() - 900) * 1e9))
        mock_result = {
            "status": "success",
            "data": {"result": [{"values": [[old_ns, "log line"]]}]},
        }
        with (
            patch("helpers.query_loki", return_value=mock_result) as mock_query,
            patch("helpers.time.sleep"),
            pytest.raises(AssertionError, match="stale"),
        ):
            assert_logs_fresh_in_loki(
                "http://loki/loki/api/v1/query_range",
                '{cluster="test"}',
                "user",
                "pass",
                max_staleness=600,
                description="test",
            )
        assert mock_query.call_count == REMOTE_RETRIES

    def test_stale_then_fresh_recovers(self) -> None:
        """Stale data on first attempt, fresh on second — should succeed."""
        old_ns = str(int((time.time() - 900) * 1e9))
        now_ns = str(int(time.time() * 1e9))
        stale_result = {
            "status": "success",
            "data": {"result": [{"values": [[old_ns, "log line"]]}]},
        }
        fresh_result = {
            "status": "success",
            "data": {"result": [{"values": [[now_ns, "log line"]]}]},
        }

        call_count = 0

        def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return stale_result if call_count == 1 else fresh_result

        with patch("helpers.query_loki", side_effect=side_effect), patch("helpers.time.sleep"):
            assert_logs_fresh_in_loki(
                "http://loki/loki/api/v1/query_range", '{cluster="test"}', "user", "pass", description="test"
            )
        assert call_count == 2

    def test_no_streams_retries_then_raises(self) -> None:
        """Persistently empty streams should retry then raise AssertionError."""
        mock_result = {"status": "success", "data": {"result": []}}
        with (
            patch("helpers.query_loki", return_value=mock_result) as mock_query,
            patch("helpers.time.sleep"),
            pytest.raises(AssertionError, match="No log streams found"),
        ):
            assert_logs_fresh_in_loki(
                "http://loki/loki/api/v1/query_range", '{cluster="test"}', "user", "pass", description="test"
            )
        assert mock_query.call_count == REMOTE_RETRIES

    def test_no_streams_then_fresh_recovers(self) -> None:
        """Empty streams on first attempt, fresh data on second — should succeed."""
        now_ns = str(int(time.time() * 1e9))
        empty_result = {"status": "success", "data": {"result": []}}
        fresh_result = {
            "status": "success",
            "data": {"result": [{"values": [[now_ns, "log line"]]}]},
        }

        call_count = 0

        def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return empty_result if call_count == 1 else fresh_result

        with patch("helpers.query_loki", side_effect=side_effect), patch("helpers.time.sleep"):
            assert_logs_fresh_in_loki(
                "http://loki/loki/api/v1/query_range", '{cluster="test"}', "user", "pass", description="test"
            )
        assert call_count == 2

    def test_network_error_retries_then_skips(self) -> None:
        """All retries failing with network error should pytest.skip."""
        with patch("helpers.query_loki", return_value=None) as mock_query:
            mock_query.last_error = "Connection refused"
            with patch("helpers.time.sleep"), pytest.raises(pytest.skip.Exception):
                assert_logs_fresh_in_loki(
                    "http://loki/loki/api/v1/query_range",
                    '{cluster="test"}',
                    "user",
                    "pass",
                    description="test",
                )
            assert mock_query.call_count == REMOTE_RETRIES

    def test_retry_succeeds_on_second_attempt(self) -> None:
        """Should succeed if second attempt returns fresh data."""
        now_ns = str(int(time.time() * 1e9))
        fresh_result = {
            "status": "success",
            "data": {"result": [{"values": [[now_ns, "log line"]]}]},
        }

        call_count = 0

        def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return None
            return fresh_result

        with patch("helpers.query_loki", side_effect=side_effect) as mock_query:
            mock_query.last_error = "timeout"
            with patch("helpers.time.sleep"):
                assert_logs_fresh_in_loki(
                    "http://loki/loki/api/v1/query_range",
                    '{cluster="test"}',
                    "user",
                    "pass",
                    description="test",
                )
        assert call_count == 2

    def test_streams_with_no_values_retries(self) -> None:
        """Streams present but with empty values should retry (no timestamp to check)."""
        mock_result = {
            "status": "success",
            "data": {"result": [{"values": []}]},
        }
        with (
            patch("helpers.query_loki", return_value=mock_result) as mock_query,
            patch("helpers.time.sleep"),
            pytest.raises(pytest.skip.Exception),
        ):
            assert_logs_fresh_in_loki(
                "http://loki/loki/api/v1/query_range", '{cluster="test"}', "user", "pass", description="test"
            )
        assert mock_query.call_count == REMOTE_RETRIES
