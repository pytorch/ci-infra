"""Grafana Cloud remote query helpers — Mimir (metrics) and Loki (logs)."""

from __future__ import annotations

import base64
import json
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import TypeVar

from .cli import run_kubectl
from .retry import BACKOFF_ATTEMPTS, BACKOFF_DELAYS

# Re-exported for callers that still import the constant.
# Backed by the unified backoff primitive — total attempt count varies by
# environment (6 local, 8 CI) to match retry_with_backoff's schedule.
REMOTE_RETRIES = BACKOFF_ATTEMPTS

T = TypeVar("T")

__all__ = [
    "REMOTE_RETRIES",
    "_urlopen_no_proxy",
    "assert_logs_fresh_in_loki",
    "assert_metric_fresh_in_mimir",
    "fetch_grafana_cloud_credentials",
    "loki_read_url",
    "mimir_read_url",
    "query_loki",
    "query_mimir",
    "retry_query_with_backoff",
]


def mimir_read_url(write_url: str) -> str:
    """Derive Mimir read endpoint from the write URL.

    Write: https://prometheus-prod-36-prod-us-west-0.grafana.net/api/prom/push
    Read:  https://prometheus-prod-36-prod-us-west-0.grafana.net/api/prom/api/v1/query
    """
    base = write_url.rstrip("/")
    if base.endswith("/push"):
        base = base[: -len("/push")]
    return f"{base}/api/v1/query"


def loki_read_url(write_url: str) -> str:
    """Derive Loki read endpoint from the write URL.

    Write: https://logs-prod-021.grafana.net/loki/api/v1/push
    Read:  https://logs-prod-021.grafana.net/loki/api/v1/query_range
    """
    base = write_url.rstrip("/")
    if base.endswith("/push"):
        base = base[: -len("/push")]
    return f"{base}/query_range"


def fetch_grafana_cloud_credentials(
    namespace: str,
    username_key: str,
    password_key: str,
    secret_name: str = "grafana-cloud-credentials",
) -> tuple[str, str] | None:
    """Fetch Grafana Cloud credentials from a Kubernetes secret.

    Returns (username, password) or None if the secret doesn't exist
    or cannot be decoded.
    """
    try:
        secret = run_kubectl(["get", "secret", secret_name], namespace=namespace)
        data = secret.get("data", {})
        if username_key not in data or password_key not in data:
            return None
        username = base64.b64decode(data[username_key]).decode()
        password = base64.b64decode(data[password_key]).decode()
        return (username, password)
    except Exception:
        return None


def _urlopen_no_proxy(req: urllib.request.Request, timeout: int = 30):
    """Open a URL request bypassing any configured HTTP(S) proxy."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return opener.open(req, timeout=timeout)


def query_mimir(url: str, promql: str, username: str, password: str, timeout: int = 30) -> dict | None:
    """Query Grafana Cloud Mimir (Prometheus-compatible API). Returns None on error."""
    full_url = f"{url}?query={urllib.parse.quote(promql)}"
    auth = base64.b64encode(f"{username}:{password}".encode()).decode()
    req = urllib.request.Request(full_url, headers={"Authorization": f"Basic {auth}"})
    try:
        with _urlopen_no_proxy(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except Exception as exc:
        query_mimir.last_error = str(exc)
        return None


query_mimir.last_error = ""


def query_loki(url: str, logql: str, username: str, password: str, timeout: int = 30) -> dict | None:
    """Query Grafana Cloud Loki (LogQL query_range). Returns None on error."""
    now = int(time.time())
    params = urllib.parse.urlencode(
        {
            "query": logql,
            "start": str(now - 3600),
            "end": str(now),
            "limit": "1",
            "direction": "backward",
        }
    )
    full_url = f"{url}?{params}"
    auth = base64.b64encode(f"{username}:{password}".encode()).decode()
    req = urllib.request.Request(full_url, headers={"Authorization": f"Basic {auth}"})
    try:
        with _urlopen_no_proxy(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except Exception as exc:
        query_loki.last_error = str(exc)
        return None


query_loki.last_error = ""


def retry_query_with_backoff(
    query: Callable[[], T | None],
    *,
    delays: list[float] | None = None,
) -> T | None:
    """Retry a query function until it returns a non-None result.

    ``query()`` must return either the result on success, or ``None`` on
    transient failure (which sets ``query.last_error`` per the convention
    used by :func:`query_mimir` / :func:`query_loki`). Retries using the
    same backoff schedule as :func:`helpers.retry.retry_with_backoff`.

    Returns the final result (which may still be ``None`` if all attempts
    failed — caller is responsible for classification, e.g. ``pytest.skip``).
    """
    if delays is None:
        delays = BACKOFF_DELAYS
    for i in range(len(delays) + 1):
        result = query()
        if result is not None:
            return result
        if i < len(delays):
            time.sleep(delays[i])
    return None


def assert_metric_fresh_in_mimir(
    read_url: str,
    promql: str,
    username: str,
    password: str,
    max_staleness: int = 600,
    description: str = "",
) -> None:
    """Query Mimir for a PromQL expression and assert fresh data exists.

    Retries on network errors OR staleness using the unified exponential
    backoff schedule (BACKOFF_ATTEMPTS total attempts). This handles
    transient gaps from pipeline restarts (e.g. Alloy pod churn) — if
    data is stale on the first check, the pipeline may still be
    catching up.

    Raises AssertionError if metric is still missing or stale after all retries.
    Raises pytest.skip if all retries fail with network errors.
    """
    import pytest

    label = description or promql
    state: dict = {"last_age": None, "last_err": ""}

    def fetch_and_validate() -> dict | None:
        """Fetch from Mimir and return the result, or None for any
        retryable transient failure (network error, empty series, stale data).
        Updates ``state`` so the post-loop classification has the right info.
        Raises AssertionError immediately for non-retryable failures
        (e.g. Mimir returns status != 'success').
        """
        result = query_mimir(read_url, promql, username, password)
        if result is None:
            state["last_err"] = getattr(query_mimir, "last_error", "unknown")
            return None

        status = result.get("status", "")
        assert status == "success", f"[{label}] Mimir query returned status '{status}'"

        results = result.get("data", {}).get("result", [])
        if len(results) == 0:
            state["last_err"] = "no metric series found"
            return None

        newest_ts = max(float(r["value"][0]) for r in results if r.get("value"))
        age = time.time() - newest_ts
        state["last_age"] = age
        if age < max_staleness:
            return result
        state["last_err"] = f"stale ({age:.0f}s)"
        return None

    final = retry_query_with_backoff(fetch_and_validate)
    if final is not None:
        return

    # All attempts exhausted — classify the failure.
    last_age = state["last_age"]
    last_err = state["last_err"]
    if last_age is not None:
        # We did get a response at some point — staleness is the failure mode.
        assert last_age < max_staleness, (
            f"[{label}] Metric is stale after {REMOTE_RETRIES} attempts: "
            f"newest sample is {last_age:.0f}s old (threshold: {max_staleness}s)"
        )
    if "no metric series found" in last_err:
        raise AssertionError(f"[{label}] No metric series found after {REMOTE_RETRIES} attempts")
    pytest.skip(f"[{label}] Mimir unreachable after {REMOTE_RETRIES} attempts: {last_err}")


def assert_logs_fresh_in_loki(
    read_url: str,
    logql: str,
    username: str,
    password: str,
    max_staleness: int = 600,
    description: str = "",
) -> None:
    """Query Loki for a LogQL expression and assert fresh log streams exist.

    Retries on network errors OR staleness using the unified exponential
    backoff schedule (BACKOFF_ATTEMPTS total attempts). This handles
    transient gaps from pipeline restarts (e.g. Alloy DaemonSet pod
    churn) — if data is stale on the first check, the pipeline may still
    be catching up.

    Raises AssertionError if logs are still missing or stale after all retries.
    Raises pytest.skip if all retries fail with network errors.
    """
    import pytest

    label = description or logql
    state: dict = {"last_age": None, "last_err": ""}

    def fetch_and_validate() -> dict | None:
        """Fetch from Loki and return the result, or None for any
        retryable transient failure. Updates ``state`` for post-loop
        classification. Raises AssertionError for non-retryable failures.
        """
        result = query_loki(read_url, logql, username, password)
        if result is None:
            state["last_err"] = getattr(query_loki, "last_error", "unknown")
            return None

        status = result.get("status", "")
        assert status == "success", f"[{label}] Loki query returned status '{status}'"

        streams = result.get("data", {}).get("result", [])
        if len(streams) == 0:
            state["last_err"] = "no log streams found"
            return None

        # Loki query_range returns streams with "values": [[nanosecond_ts, line], ...]
        newest_ns = 0
        for stream in streams:
            for ts_ns, _ in stream.get("values", []):
                newest_ns = max(newest_ns, int(ts_ns))

        if newest_ns == 0:
            state["last_err"] = "no timestamps in log streams"
            return None

        age = time.time() - (newest_ns / 1e9)
        state["last_age"] = age
        if age < max_staleness:
            return result
        state["last_err"] = f"stale ({age:.0f}s)"
        return None

    final = retry_query_with_backoff(fetch_and_validate)
    if final is not None:
        return

    # All attempts exhausted — classify the failure.
    last_age = state["last_age"]
    last_err = state["last_err"]
    if last_age is not None:
        assert last_age < max_staleness, (
            f"[{label}] Logs are stale after {REMOTE_RETRIES} attempts: "
            f"newest entry is {last_age:.0f}s old (threshold: {max_staleness}s)"
        )
    if "no log streams found" in last_err:
        raise AssertionError(f"[{label}] No log streams found after {REMOTE_RETRIES} attempts")
    pytest.skip(f"[{label}] Loki unreachable after {REMOTE_RETRIES} attempts: {last_err}")
