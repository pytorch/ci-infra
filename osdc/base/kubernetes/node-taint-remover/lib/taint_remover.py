#!/usr/bin/env python3
"""Remove a specific startup taint from this node via the Kubernetes API.

Retries forever on transient errors (network timeouts, HTTP 429/5xx), backing
off exponentially with jitter and honouring Retry-After, so a fleet of
throttled nodes does not keep hammering an overloaded API server. Idempotent:
returns success if the taint is already absent.

Usage: taint_remover.py <taint-key>

Environment:
  NODE_NAME (required, from Downward API)
  KUBERNETES_SERVICE_HOST / _PORT (set automatically inside cluster)

Uses RFC 6902 JSON Patch (test + remove by index) for race-safety vs other
controllers (Karpenter, kubelet) that may add/remove taints concurrently.
"""

from __future__ import annotations

import http.client
import json
import logging
import os
import random
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

TOKEN_PATH = Path("/var/run/secrets/kubernetes.io/serviceaccount/token")
CA_PATH = Path("/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")

# Transient failures (HTTP 429/5xx, timeouts, connection errors) usually mean
# the API server is shedding load: back off exponentially (with jitter, so
# nodes launched in one burst fall out of phase) up to the same 5-minute
# ceiling kubelet uses for CrashLoopBackOff. A jittered Retry-After floor can
# exceed the cap (see _backoff).
BACKOFF_BASE = 5
BACKOFF_CAP = 300

log = logging.getLogger("taint_remover")


class TransientApiError(Exception):
    """Non-2xx response that may succeed if retried (HTTP 429/5xx)."""

    def __init__(self, message: str, retry_after: int | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class PermanentApiError(Exception):
    """Non-2xx response or config problem that will not succeed on retry."""


def _k8s_api() -> str:
    """Build the Kubernetes API base URL from in-cluster env vars (IPv6-safe)."""
    host = os.environ.get("KUBERNETES_SERVICE_HOST")
    port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
    if not host:
        raise RuntimeError("KUBERNETES_SERVICE_HOST not set — not running inside a pod?")
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"https://{host}:{port}"


def _read_token() -> str:
    if not TOKEN_PATH.exists():
        raise RuntimeError(f"ServiceAccount token not found at {TOKEN_PATH}")
    return TOKEN_PATH.read_text().strip()


def _ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if CA_PATH.exists():
        ctx.load_verify_locations(cafile=str(CA_PATH))
    return ctx


def _node_name() -> str:
    name = os.environ.get("NODE_NAME")
    if not name:
        raise RuntimeError("NODE_NAME env var not set (Downward API spec.nodeName)")
    return name


def _request(
    method: str, url: str, token: str, ctx: ssl.SSLContext, body: bytes | None = None, content_type: str | None = None
) -> tuple[int, bytes, int | None]:
    """Single HTTP call. Returns (status, body, retry_after).

    `retry_after` is the response's Retry-After header in seconds, or None.
    Raises on transport errors so caller can retry.
    """
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if content_type:
        headers["Content-Type"] = content_type
    req = urllib.request.Request(url, data=body, method=method, headers=headers)  # noqa: S310
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:  # noqa: S310
            return resp.status, resp.read(), None
    except urllib.error.HTTPError as e:
        return e.code, e.read() if hasattr(e, "read") else b"", _parse_retry_after(e.headers)


def _parse_retry_after(headers) -> int | None:
    """Retry-After in seconds, or None if absent or not delta-seconds (HTTP-date is ignored)."""
    value = headers.get("Retry-After") if headers is not None else None
    try:
        return max(0, int(value)) if value is not None else None
    except ValueError:
        return None


def _is_server_backpressure(status: int) -> bool:
    return 500 <= status < 600 or status == 429


def _get_node(token: str, ctx: ssl.SSLContext) -> dict:
    """GET /api/v1/nodes/$NODE_NAME → parsed JSON dict."""
    url = f"{_k8s_api()}/api/v1/nodes/{_node_name()}"
    status, body, retry_after = _request("GET", url, token, ctx)
    if status == 200:
        return json.loads(body)
    if _is_server_backpressure(status):
        raise TransientApiError(f"GET node returned HTTP {status}: {body!r}", retry_after)
    raise PermanentApiError(f"GET node returned HTTP {status}: {body!r}")


def _find_taint_index(node: dict, key: str) -> int | None:
    taints = node.get("spec", {}).get("taints", []) or []
    for i, t in enumerate(taints):
        if t.get("key") == key:
            return i
    return None


def _patch_remove_taint(token: str, ctx: ssl.SSLContext, index: int, key: str) -> tuple[int, bytes, int | None]:
    """PATCH the node with a JSON Patch [test, remove] pair. Returns (status, body, retry_after)."""
    patch = [
        {"op": "test", "path": f"/spec/taints/{index}/key", "value": key},
        {"op": "remove", "path": f"/spec/taints/{index}"},
    ]
    url = f"{_k8s_api()}/api/v1/nodes/{_node_name()}"
    body = json.dumps(patch).encode("utf-8")
    return _request("PATCH", url, token, ctx, body=body, content_type="application/json-patch+json")


def _backoff(attempt: int, retry_after: int | None = None) -> float:
    """Delay before retrying after a transient failure.

    Exponential from BACKOFF_BASE, capped at BACKOFF_CAP, with "equal jitter"
    (uniform in [d/2, d]) so the delay still grows but nodes throttled
    together spread out. Never shorter than the server's Retry-After (capped
    at BACKOFF_CAP), which is itself jittered up to 1.5x so nodes given the
    same Retry-After do not retry in lockstep; the delay can therefore exceed
    BACKOFF_CAP, by up to half again.
    """
    ceiling = min(BACKOFF_CAP, BACKOFF_BASE * 2**attempt)
    delay = random.uniform(ceiling / 2, ceiling)  # noqa: S311 — jitter, not crypto
    if retry_after is not None:
        floor = min(retry_after, BACKOFF_CAP)
        delay = max(delay, floor * random.uniform(1.0, 1.5))  # noqa: S311 — jitter, not crypto
    return delay


def remove_taint_forever(taint_key: str) -> None:
    """Remove `taint_key` from this node. Retries forever on transient errors.

    Exits the function (returns) once the taint is gone (or was never present).
    Raises PermanentApiError on permanent errors (a non-2xx from the GET
    other than 429/5xx, including 401; or a 4xx from the PATCH other than
    401/409/422/429), and RuntimeError on configuration problems (missing
    KUBERNETES_SERVICE_HOST, NODE_NAME or SA token).
    """
    ctx = _ssl_context()

    # Consecutive transient failures; drives the backoff. Not reset by the
    # immediate 409/422 retries, so an overloaded API server is never
    # hit faster just because a race happened in between.
    attempt = 0
    # Whether the previous PATCH lost a race (409/422). A single race is
    # retried at once; a second in a row backs off, so a PATCH that keeps
    # being rejected cannot spin a tight GET/PATCH loop.
    raced = False
    # Index of the taint from the last GET. Kept across a PATCH that failed
    # with 429/5xx or a network error, since a re-GET would only add load to
    # an API server that is already struggling. Usually the write was
    # refused and spec.taints is unchanged; if it did land (e.g. a 504 after
    # etcd committed it), the PATCH's JSON Patch `test` op fails with 422,
    # which forces a re-read that finds the taint gone.
    index = None
    while True:
        # Re-read on every iteration so kubelet's in-place rotation of the
        # projected SA token is picked up on long retry sequences. Same
        # rationale for 401 below.
        token = _read_token()
        retry_after = None
        raced_before, raced = raced, False
        try:
            if index is None:
                node = _get_node(token, ctx)
                index = _find_taint_index(node, taint_key)
                if index is None:
                    log.info("Taint '%s' already absent on node — nothing to do.", taint_key)
                    return

            status, body, retry_after = _patch_remove_taint(token, ctx, index, taint_key)
            if status == 200:
                log.info("Removed taint '%s' from node.", taint_key)
                return
            if status in (409, 422):
                # 409: resourceVersion conflict. 422: JSON Patch `test` op
                # failed because the taint index shifted under us (Karpenter
                # or kubelet mutated spec.taints concurrently). Both are
                # benign — re-read the node and retry, at once the first time.
                index = None
                raced = True
                if not raced_before:
                    log.info("Taint set changed concurrently (HTTP %s) — re-reading and retrying.", status)
                    continue
                log.warning("PATCH rejected again (HTTP %s): %s — backing off.", status, body)
            elif status == 401:
                # Back off and re-read the node rather than `continue`: a
                # token that stays rejected must not be retried in a tight
                # loop, and a 401 on the re-GET is permanent, so a token
                # that never recovers ends the process.
                log.info("Unauthorized (HTTP 401) — backing off and retrying with a fresh token.")
                index = None
            elif _is_server_backpressure(status):
                # 429: API Priority and Fairness / etcd throttling under a
                # mass node launch. Retry the PATCH (not the GET) after the
                # backoff below.
                log.warning("PATCH returned HTTP %s: %s — retrying.", status, body)
            elif 400 <= status < 500:
                raise PermanentApiError(f"Permanent error from K8s API (HTTP {status}): {body!r}")
            else:
                log.warning("Unexpected HTTP %s from PATCH: %s — retrying.", status, body)
                index = None
        except (
            urllib.error.URLError,
            TimeoutError,
            ConnectionError,
            OSError,
            http.client.IncompleteRead,
            http.client.BadStatusLine,
        ) as e:
            # Timeouts and dropped or truncated connections are often the
            # same overload as a 429, just without a status code — back off
            # the same way. Other http.client errors (e.g. InvalidURL from a
            # bad KUBERNETES_SERVICE_PORT) are configuration problems and
            # still end the process.
            log.warning("Transient network error: %s — retrying.", e)
        except TransientApiError as e:
            log.warning("Transient API error: %s — retrying.", e)
            retry_after = e.retry_after
        except PermanentApiError:
            raise
        except Exception:
            log.exception("Unexpected exception in remove_taint_forever — re-raising.")
            raise

        sleep_s = _backoff(attempt, retry_after)
        log.info("Backing off %.0fs before retry %d.", sleep_s, attempt + 1)
        time.sleep(sleep_s)
        attempt += 1


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if len(sys.argv) != 2 or not sys.argv[1].strip():
        log.error("Usage: %s <taint-key>", sys.argv[0])
        return 1
    taint_key = sys.argv[1].strip()
    try:
        remove_taint_forever(taint_key)
        return 0
    except (PermanentApiError, RuntimeError) as e:
        log.error("%s", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
