"""Unit tests for the retry_with_backoff primitive.

Patches the module-level ``time.sleep`` so tests run instantaneously while
still asserting the EXACT delay schedule that production code would sleep for.
"""

from __future__ import annotations

from unittest.mock import MagicMock, call, patch

import pytest
from helpers.retry import (
    BACKOFF_ATTEMPTS,
    BACKOFF_DELAYS,
    BACKOFF_DELAYS_CI,
    BACKOFF_DELAYS_LOCAL,
    _ci_enabled,
    retry_with_backoff,
)

# ============================================================================
# Constants
# ============================================================================


class TestBackoffConstants:
    """Verify the backoff delay schedules are exactly as documented."""

    def test_local_delays_exact_values(self) -> None:
        assert BACKOFF_DELAYS_LOCAL == [10.0, 17.0, 28.9, 49.1, 83.4]

    def test_local_delays_length(self) -> None:
        # 5 sleep delays => 6 total attempts
        assert len(BACKOFF_DELAYS_LOCAL) == 5

    def test_ci_delays_exact_values(self) -> None:
        assert BACKOFF_DELAYS_CI == [10.0, 17.0, 28.9, 49.1, 83.4, 141.78, 241.03]

    def test_ci_delays_length(self) -> None:
        # 7 sleep delays => 8 total attempts
        assert len(BACKOFF_DELAYS_CI) == 7

    def test_ci_extends_local_prefix(self) -> None:
        """The first 5 CI delays must match BACKOFF_DELAYS_LOCAL exactly."""
        assert BACKOFF_DELAYS_CI[: len(BACKOFF_DELAYS_LOCAL)] == BACKOFF_DELAYS_LOCAL

    def test_ci_extension_follows_1_7x_factor(self) -> None:
        """The two extra CI delays continue the ~1.7x exponential curve."""
        # Index 5: previous was 83.4, expected ~141.78
        assert BACKOFF_DELAYS_CI[5] == pytest.approx(BACKOFF_DELAYS_CI[4] * 1.7, rel=0.01)
        # Index 6: previous was 141.78, expected ~241.03
        assert BACKOFF_DELAYS_CI[6] == pytest.approx(BACKOFF_DELAYS_CI[5] * 1.7, rel=0.01)

    def test_local_curve_follows_1_7x_factor(self) -> None:
        """Each local delay is ~1.7x the previous."""
        for i in range(1, len(BACKOFF_DELAYS_LOCAL)):
            assert BACKOFF_DELAYS_LOCAL[i] == pytest.approx(BACKOFF_DELAYS_LOCAL[i - 1] * 1.7, rel=0.01)

    def test_backoff_attempts_matches_default_delays(self) -> None:
        assert BACKOFF_ATTEMPTS == len(BACKOFF_DELAYS) + 1

    def test_default_delays_is_local_or_ci(self) -> None:
        """At module load, BACKOFF_DELAYS resolves to one of the two known schedules."""
        assert BACKOFF_DELAYS in (BACKOFF_DELAYS_LOCAL, BACKOFF_DELAYS_CI)


# ============================================================================
# retry_with_backoff — happy path
# ============================================================================


class TestRetryWithBackoffSuccess:
    """Tests where check() eventually (or immediately) succeeds."""

    @patch("helpers.retry.time.sleep")
    def test_returns_immediately_when_check_passes(self, mock_sleep: MagicMock) -> None:
        check = MagicMock(return_value="ok")
        refresh = MagicMock()

        result = retry_with_backoff(check, refresh=refresh)

        assert result == "ok"
        assert check.call_count == 1
        assert mock_sleep.call_count == 0
        assert refresh.call_count == 0

    @patch("helpers.retry.time.sleep")
    def test_no_refresh_called_when_no_refresh_provided(self, mock_sleep: MagicMock) -> None:
        check = MagicMock(return_value=None)

        # Just confirm we don't blow up when refresh=None and check passes first try.
        retry_with_backoff(check)

        assert check.call_count == 1
        assert mock_sleep.call_count == 0

    @patch("helpers.retry.time.sleep")
    def test_succeeds_on_second_attempt(self, mock_sleep: MagicMock) -> None:
        check = MagicMock(side_effect=[AssertionError("first failure"), "ok"])
        refresh = MagicMock()
        delays = [1.0, 2.0, 4.0]

        result = retry_with_backoff(check, refresh=refresh, delays=delays)

        assert result == "ok"
        assert check.call_count == 2
        # Slept exactly once between the two attempts, with delays[0].
        mock_sleep.assert_called_once_with(1.0)
        # Refresh called exactly once, between the two attempts.
        assert refresh.call_count == 1

    @patch("helpers.retry.time.sleep")
    def test_succeeds_on_last_attempt(self, mock_sleep: MagicMock) -> None:
        delays = [1.0, 2.0, 4.0]
        # 3 failures, then success on the 4th (final allowed) attempt.
        check = MagicMock(side_effect=[AssertionError("a"), AssertionError("b"), AssertionError("c"), "ok"])
        refresh = MagicMock()

        result = retry_with_backoff(check, refresh=refresh, delays=delays)

        assert result == "ok"
        assert check.call_count == 4  # len(delays) + 1
        # Slept 3 times, once between each failed attempt and the next.
        assert mock_sleep.call_args_list == [call(1.0), call(2.0), call(4.0)]
        # Refresh called 3 times, once after each sleep.
        assert refresh.call_count == 3

    @patch("helpers.retry.time.sleep")
    def test_returns_value_from_check(self, mock_sleep: MagicMock) -> None:
        """retry_with_backoff returns whatever the successful check() returns."""
        sentinel = {"complex": ["return", "value"], "n": 42}
        check = MagicMock(return_value=sentinel)

        result = retry_with_backoff(check)

        assert result is sentinel


# ============================================================================
# retry_with_backoff — exhaustion
# ============================================================================


class TestRetryWithBackoffExhaustion:
    """Tests where every attempt fails and the LAST AssertionError is re-raised."""

    @patch("helpers.retry.time.sleep")
    def test_raises_last_assertion_error(self, mock_sleep: MagicMock) -> None:
        delays = [1.0, 2.0]
        check = MagicMock(
            side_effect=[
                AssertionError("first"),
                AssertionError("second"),
                AssertionError("LAST"),
            ]
        )

        with pytest.raises(AssertionError, match="LAST"):
            retry_with_backoff(check, delays=delays)

        # All 3 attempts ran (len(delays) + 1).
        assert check.call_count == 3
        # Sleeps happened only between attempts, not after the last one.
        assert mock_sleep.call_args_list == [call(1.0), call(2.0)]

    @patch("helpers.retry.time.sleep")
    def test_does_not_call_refresh_after_last_attempt(self, mock_sleep: MagicMock) -> None:
        """No sleep, no refresh after the FINAL failing attempt — pointless work."""
        delays = [1.0, 2.0]
        check = MagicMock(side_effect=AssertionError("always fail"))
        refresh = MagicMock()

        with pytest.raises(AssertionError):
            retry_with_backoff(check, refresh=refresh, delays=delays)

        # 3 attempts, 2 sleeps, 2 refreshes — never one extra sleep/refresh after the
        # final failure.
        assert check.call_count == 3
        assert mock_sleep.call_count == 2
        assert refresh.call_count == 2

    @patch("helpers.retry.time.sleep")
    def test_total_attempt_count_equals_delays_plus_one(self, mock_sleep: MagicMock) -> None:
        """For any delays list, total attempts = len(delays) + 1."""
        for n in (0, 1, 2, 3, 5, 7):
            delays = [1.0] * n
            check = MagicMock(side_effect=AssertionError("fail"))
            with pytest.raises(AssertionError):
                retry_with_backoff(check, delays=delays)
            assert check.call_count == n + 1, f"with delays of length {n}"


# ============================================================================
# retry_with_backoff — refresh ordering
# ============================================================================


class TestRefreshOrdering:
    """refresh() must be called AFTER sleep, BEFORE the next check()."""

    @patch("helpers.retry.time.sleep")
    def test_refresh_called_after_sleep_before_next_check(self, mock_sleep: MagicMock) -> None:
        events: list[str] = []
        delays = [1.0, 2.0]

        def check_side_effect():
            events.append("check")
            raise AssertionError("nope")

        def refresh_side_effect():
            events.append("refresh")

        def sleep_side_effect(_seconds):
            events.append("sleep")

        mock_sleep.side_effect = sleep_side_effect
        check = MagicMock(side_effect=check_side_effect)
        refresh = MagicMock(side_effect=refresh_side_effect)

        with pytest.raises(AssertionError):
            retry_with_backoff(check, refresh=refresh, delays=delays)

        # 3 attempts, with sleep+refresh sandwiched between each pair.
        assert events == [
            "check",
            "sleep",
            "refresh",
            "check",
            "sleep",
            "refresh",
            "check",
        ]


# ============================================================================
# retry_with_backoff — non-AssertionError propagation
# ============================================================================


class TestNonAssertionErrorPropagation:
    """Anything other than AssertionError must propagate immediately."""

    @patch("helpers.retry.time.sleep")
    def test_keyerror_propagates_immediately(self, mock_sleep: MagicMock) -> None:
        check = MagicMock(side_effect=KeyError("missing"))
        refresh = MagicMock()

        with pytest.raises(KeyError, match="missing"):
            retry_with_backoff(check, refresh=refresh, delays=[1.0, 2.0, 4.0])

        # Only one attempt, no sleeps, no refresh.
        assert check.call_count == 1
        assert mock_sleep.call_count == 0
        assert refresh.call_count == 0

    @patch("helpers.retry.time.sleep")
    def test_valueerror_propagates_immediately(self, mock_sleep: MagicMock) -> None:
        check = MagicMock(side_effect=ValueError("bad input"))

        with pytest.raises(ValueError, match="bad input"):
            retry_with_backoff(check, delays=[1.0, 2.0])

        assert check.call_count == 1
        assert mock_sleep.call_count == 0

    @patch("helpers.retry.time.sleep")
    def test_runtime_error_propagates_immediately(self, mock_sleep: MagicMock) -> None:
        check = MagicMock(side_effect=RuntimeError("boom"))

        with pytest.raises(RuntimeError, match="boom"):
            retry_with_backoff(check)

        assert check.call_count == 1
        assert mock_sleep.call_count == 0

    @patch("helpers.retry.time.sleep")
    def test_assertion_then_keyerror_does_not_swallow(self, mock_sleep: MagicMock) -> None:
        """If retry recovers from an AssertionError but then hits a non-AssertionError,
        the non-AssertionError must propagate."""
        check = MagicMock(side_effect=[AssertionError("transient"), KeyError("real")])

        with pytest.raises(KeyError, match="real"):
            retry_with_backoff(check, delays=[1.0, 2.0])

        assert check.call_count == 2
        # Slept once between the AssertionError and the KeyError.
        assert mock_sleep.call_count == 1


# ============================================================================
# retry_with_backoff — schedule fidelity
# ============================================================================


class TestScheduleFidelity:
    """Verify that the EXACT default schedule is used when delays is None."""

    @patch("helpers.retry.time.sleep")
    def test_local_schedule_used_when_delays_is_local(self, mock_sleep: MagicMock) -> None:
        """Passing BACKOFF_DELAYS_LOCAL explicitly produces exactly that sleep sequence."""
        check = MagicMock(side_effect=AssertionError("fail"))

        with pytest.raises(AssertionError):
            retry_with_backoff(check, delays=BACKOFF_DELAYS_LOCAL)

        # 6 attempts, 5 sleeps with the exact LOCAL schedule.
        assert check.call_count == 6
        assert mock_sleep.call_args_list == [call(d) for d in BACKOFF_DELAYS_LOCAL]

    @patch("helpers.retry.time.sleep")
    def test_ci_schedule_used_when_delays_is_ci(self, mock_sleep: MagicMock) -> None:
        """Passing BACKOFF_DELAYS_CI explicitly produces exactly that sleep sequence."""
        check = MagicMock(side_effect=AssertionError("fail"))

        with pytest.raises(AssertionError):
            retry_with_backoff(check, delays=BACKOFF_DELAYS_CI)

        # 8 attempts, 7 sleeps with the exact CI schedule.
        assert check.call_count == 8
        assert mock_sleep.call_args_list == [call(d) for d in BACKOFF_DELAYS_CI]

    @patch("helpers.retry.time.sleep")
    def test_default_uses_module_backoff_delays(self, mock_sleep: MagicMock) -> None:
        """Calling without delays= uses the module-level BACKOFF_DELAYS."""
        check = MagicMock(side_effect=AssertionError("fail"))

        with pytest.raises(AssertionError):
            retry_with_backoff(check)

        assert check.call_count == len(BACKOFF_DELAYS) + 1
        assert mock_sleep.call_args_list == [call(d) for d in BACKOFF_DELAYS]

    @patch("helpers.retry.time.sleep")
    def test_custom_delays_override_default(self, mock_sleep: MagicMock) -> None:
        """A custom delays list bypasses BACKOFF_DELAYS entirely."""
        check = MagicMock(side_effect=AssertionError("fail"))
        custom = [0.1, 0.2, 0.3, 0.4]

        with pytest.raises(AssertionError):
            retry_with_backoff(check, delays=custom)

        assert check.call_count == len(custom) + 1
        assert mock_sleep.call_args_list == [call(d) for d in custom]

    @patch("helpers.retry.time.sleep")
    def test_empty_delays_means_one_attempt(self, mock_sleep: MagicMock) -> None:
        """delays=[] => exactly one attempt, no sleeps, no retries."""
        check = MagicMock(side_effect=AssertionError("fail"))

        with pytest.raises(AssertionError, match="fail"):
            retry_with_backoff(check, delays=[])

        assert check.call_count == 1
        assert mock_sleep.call_count == 0


# ============================================================================
# _ci_enabled — strict truthy parsing of the CI env var
# ============================================================================


class TestCiEnabled:
    """Verify CI env var parsing is strict (no false-positives on 'false'/'0'/'no').

    BACKOFF_DELAYS is resolved at module import time, so we test the helper
    function directly via os.environ monkeypatching rather than re-importing.
    """

    @pytest.mark.parametrize(
        "value",
        ["true", "1", "yes", "TRUE", "True", "Yes", "YES", " true ", "TrUe"],
    )
    def test_truthy_values_enable_ci(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        monkeypatch.setenv("CI", value)
        assert _ci_enabled() is True, f"CI={value!r} should enable CI schedule"

    @pytest.mark.parametrize(
        "value",
        ["false", "0", "no", "FALSE", "False", "No", "NO", "off", "", "  ", "anything-else"],
    )
    def test_falsy_values_disable_ci(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        monkeypatch.setenv("CI", value)
        assert _ci_enabled() is False, f"CI={value!r} should NOT enable CI schedule"

    def test_unset_disables_ci(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CI", raising=False)
        assert _ci_enabled() is False


# ============================================================================
# retry_with_backoff — refresh failure handling
# ============================================================================


class TestRefreshFailureHandling:
    """A raising refresh() must NOT mask the original AssertionError.

    The contract: when refresh() throws (e.g. transient kubectl timeout), the
    loop logs a one-line warning to stderr and continues with stale state. The
    next check() then re-raises the AssertionError — so the caller sees the
    REAL failure, not the refresh's transient hiccup.
    """

    @patch("helpers.retry.time.sleep")
    def test_refresh_failure_does_not_propagate(
        self,
        mock_sleep: MagicMock,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A raising refresh() lets the loop continue to the next check()."""
        check = MagicMock(side_effect=AssertionError("real failure"))
        refresh = MagicMock(side_effect=RuntimeError("kubectl timed out"))
        delays = [1.0, 2.0]

        with pytest.raises(AssertionError, match="real failure"):
            retry_with_backoff(check, refresh=refresh, delays=delays)

        # All 3 attempts ran even though every refresh failed.
        assert check.call_count == 3
        # Refresh attempted twice (once per gap between attempts).
        assert refresh.call_count == 2
        captured = capsys.readouterr()
        # Warning printed to stderr for each refresh failure.
        assert captured.err.count("retry_with_backoff: refresh failed") == 2
        assert "kubectl timed out" in captured.err

    @patch("helpers.retry.time.sleep")
    def test_refresh_failure_then_check_succeeds(
        self,
        mock_sleep: MagicMock,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """If a later check() succeeds, refresh failures don't abort the retry."""
        check = MagicMock(side_effect=[AssertionError("transient"), "ok"])
        refresh = MagicMock(side_effect=RuntimeError("kubectl flake"))
        delays = [1.0, 2.0]

        result = retry_with_backoff(check, refresh=refresh, delays=delays)

        assert result == "ok"
        assert check.call_count == 2
        assert refresh.call_count == 1
        captured = capsys.readouterr()
        assert "retry_with_backoff: refresh failed" in captured.err

    @patch("helpers.retry.time.sleep")
    def test_warning_includes_attempt_number(
        self,
        mock_sleep: MagicMock,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """The warning line includes the attempt index for debuggability."""
        check = MagicMock(side_effect=AssertionError("fail"))
        refresh = MagicMock(side_effect=ValueError("boom"))
        delays = [1.0, 2.0, 4.0]

        with pytest.raises(AssertionError):
            retry_with_backoff(check, refresh=refresh, delays=delays)

        captured = capsys.readouterr()
        # Three refresh failures, each with its own attempt number.
        assert "attempt 1/3" in captured.err
        assert "attempt 2/3" in captured.err
        assert "attempt 3/3" in captured.err

    @patch("helpers.retry.time.sleep")
    def test_intermittent_refresh_failures_continue(
        self,
        mock_sleep: MagicMock,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A single refresh failure doesn't abort — the next refresh runs normally."""
        check = MagicMock(side_effect=AssertionError("always fail"))
        refresh = MagicMock(side_effect=[RuntimeError("flake"), None, None])
        delays = [1.0, 2.0, 4.0]

        with pytest.raises(AssertionError, match="always fail"):
            retry_with_backoff(check, refresh=refresh, delays=delays)

        assert check.call_count == 4
        assert refresh.call_count == 3
        captured = capsys.readouterr()
        # Exactly one warning line — only the first refresh failed.
        assert captured.err.count("retry_with_backoff: refresh failed") == 1
