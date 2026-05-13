"""Generic retry-with-exponential-backoff primitive for smoke-test assertions.

The 1.7x backoff curve gives a much more forgiving sleep budget than the old
fixed 15s * N pattern, while keeping fast feedback for transient mismatches:

    Local: [10, 17, 28.9, 49.1, 83.4]              -> 6 attempts, ~188s budget
    CI:    [10, 17, 28.9, 49.1, 83.4, 141.78, 241] -> 8 attempts, ~571s budget

CI gets two extra attempts because cluster operations there (Karpenter
consolidation, node provisioning, image pulls from Harbor proxy cache) are
routinely 2-3x slower than on a developer laptop talking to a warm cluster.
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")

# Same 1.7x exponential curve, extended in CI to allow more time for slow recoveries.
BACKOFF_DELAYS_LOCAL: list[float] = [10.0, 17.0, 28.9, 49.1, 83.4]
BACKOFF_DELAYS_CI: list[float] = [10.0, 17.0, 28.9, 49.1, 83.4, 141.78, 241.03]


def _ci_enabled() -> bool:
    """True iff the CI env var indicates we are running in CI.

    Strict truthy check — bare ``os.environ.get("CI")`` treats every non-empty
    string as truthy, which silently selects the slow CI schedule when a
    developer exports ``CI=false`` / ``CI=0`` / ``CI=no`` to opt out locally.
    """
    return os.environ.get("CI", "").strip().lower() in ("true", "1", "yes")


# Resolved at module load. Local: 6 attempts (~188s sleep budget). CI: 8 attempts (~571s).
BACKOFF_DELAYS: list[float] = BACKOFF_DELAYS_CI if _ci_enabled() else BACKOFF_DELAYS_LOCAL
BACKOFF_ATTEMPTS = len(BACKOFF_DELAYS) + 1

__all__ = [
    "BACKOFF_ATTEMPTS",
    "BACKOFF_DELAYS",
    "BACKOFF_DELAYS_CI",
    "BACKOFF_DELAYS_LOCAL",
    "retry_with_backoff",
]


def retry_with_backoff(
    check: Callable[[], T],
    *,
    refresh: Callable[[], None] | None = None,
    delays: list[float] | None = None,
) -> T:
    """Retry ``check()`` on AssertionError with exponential backoff.

    Total attempts = ``len(delays) + 1``. Sleeps ``delays[i]`` before the
    ``(i+1)``-th attempt. If ``refresh`` is given, it is called AFTER the sleep
    so callers can re-fetch live state before the next retry. Other exceptions
    propagate immediately on first occurrence. Re-raises the LAST AssertionError
    after all attempts fail.

    Args:
        check: Zero-arg callable that performs the assertion(s) and returns
            whatever value the caller wants. Called once per attempt.
        refresh: Optional zero-arg callable invoked between attempts (after
            ``time.sleep``, before the next ``check``). Use it to re-fetch
            live cluster state so the next ``check`` sees fresh data.
        delays: Override for the default backoff schedule. Defaults to
            :data:`BACKOFF_DELAYS` (CI-aware). Pass an explicit list for
            tests or per-call tuning.

    Returns:
        Whatever ``check()`` returns on success.

    Raises:
        AssertionError: The LAST AssertionError raised by ``check()`` after
            all attempts fail.
    """
    if delays is None:
        delays = BACKOFF_DELAYS
    last_error: AssertionError | None = None
    for i in range(len(delays) + 1):
        try:
            return check()
        except AssertionError as e:
            last_error = e
            if i < len(delays):
                time.sleep(delays[i])
                if refresh is not None:
                    try:
                        refresh()
                    except Exception as refresh_err:
                        # A transient refresh failure (e.g. kubectl timeout)
                        # must not mask the underlying AssertionError. Log a
                        # one-line warning and continue with stale state —
                        # the next check() will retry the assertion. If the
                        # underlying issue persists, the AssertionError will
                        # be re-raised after exhaustion.
                        print(
                            f"retry_with_backoff: refresh failed "
                            f"(attempt {i + 1}/{len(delays)}): {refresh_err}",
                            file=sys.stderr,
                        )
    assert last_error is not None
    raise last_error
