#!/usr/bin/env python3
"""Remove a specific startup taint from this node via the Kubernetes API.

Retries forever on transient errors (network timeouts, HTTP 5xx). Idempotent:
returns success if the taint is already absent.

Usage: taint_remover.py <taint-key>

Environment:
  NODE_NAME (required, from Downward API)
  KUBERNETES_SERVICE_HOST / _PORT (set automatically inside cluster)

Uses RFC 6902 JSON Patch (test + remove by index) for race-safety vs other
controllers (Karpenter, kubelet) that may add/remove taints concurrently.

Before patching, the script verifies that the node's `instance-type` taint
is present (if the node has an `instance-type` label) — this is the OSDC
convention that ensures the node has fully registered with Karpenter
before any controller starts modifying its taint set. The guard retries
indefinitely with backoff.
"""

from __future__ import annotations

import json
import logging
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

TOKEN_PATH = Path("/var/run/secrets/kubernetes.io/serviceaccount/token")
CA_PATH = Path("/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")

INSTANCE_TYPE_LABEL = "instance-type"
INSTANCE_TYPE_TAINT_KEY = "instance-type"

BACKOFF_SCHEDULE = (5, 10, 30)

log = logging.getLogger("taint_remover")


class TransientApiError(Exception):
    """Non-2xx response that may succeed if retried (HTTP 5xx, 409, etc.)."""


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
) -> tuple[int, bytes]:
    """Single HTTP call. Returns (status, body). Raises on transport errors so caller can retry."""
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if content_type:
        headers["Content-Type"] = content_type
    req = urllib.request.Request(url, data=body, method=method, headers=headers)  # noqa: S310
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:  # noqa: S310
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read() if hasattr(e, "read") else b""


def _get_node(token: str, ctx: ssl.SSLContext) -> dict:
    """GET /api/v1/nodes/$NODE_NAME → parsed JSON dict."""
    url = f"{_k8s_api()}/api/v1/nodes/{_node_name()}"
    status, body = _request("GET", url, token, ctx)
    if status == 200:
        return json.loads(body)
    if 500 <= status < 600 or status == 429:
        raise TransientApiError(f"GET node returned HTTP {status}: {body!r}")
    raise PermanentApiError(f"GET node returned HTTP {status}: {body!r}")


def _has_instance_type_taint(node: dict) -> bool:
    """True iff the node has a taint with key 'instance-type'."""
    taints = node.get("spec", {}).get("taints", []) or []
    return any(t.get("key") == INSTANCE_TYPE_TAINT_KEY for t in taints)


def _has_instance_type_label(node: dict) -> bool:
    labels = node.get("metadata", {}).get("labels", {}) or {}
    return INSTANCE_TYPE_LABEL in labels


def _find_taint_index(node: dict, key: str) -> int | None:
    taints = node.get("spec", {}).get("taints", []) or []
    for i, t in enumerate(taints):
        if t.get("key") == key:
            return i
    return None


def _patch_remove_taint(token: str, ctx: ssl.SSLContext, index: int, key: str) -> tuple[int, bytes]:
    """PATCH the node with a JSON Patch [test, remove] pair. Returns (status, body)."""
    patch = [
        {"op": "test", "path": f"/spec/taints/{index}/key", "value": key},
        {"op": "remove", "path": f"/spec/taints/{index}"},
    ]
    url = f"{_k8s_api()}/api/v1/nodes/{_node_name()}"
    body = json.dumps(patch).encode("utf-8")
    return _request("PATCH", url, token, ctx, body=body, content_type="application/json-patch+json")


def _next_backoff(attempt: int) -> int:
    return BACKOFF_SCHEDULE[min(attempt, len(BACKOFF_SCHEDULE) - 1)]


def _wait_for_instance_type_taint(token: str, ctx: ssl.SSLContext) -> dict:
    """Block until the node's instance-type taint is present (or label absent).

    Returns the freshly-fetched node dict. Retries forever on transient errors.
    Skipped (returns immediately) if the node does not have the instance-type
    label — e.g. base infrastructure nodes that aren't Karpenter-managed.
    """
    attempt = 0
    while True:
        try:
            node = _get_node(token, ctx)
            if not _has_instance_type_label(node):
                log.info("Node has no '%s' label — skipping instance-type taint guard.", INSTANCE_TYPE_LABEL)
                return node
            if _has_instance_type_taint(node):
                return node
            log.info(
                "Node has '%s' label but no '%s' taint yet — waiting for Karpenter registration...",
                INSTANCE_TYPE_LABEL,
                INSTANCE_TYPE_TAINT_KEY,
            )
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            log.warning("Transient error during instance-type guard GET: %s — retrying.", e)
        except TransientApiError as e:
            log.warning("Transient API error during instance-type guard GET: %s — retrying.", e)
        sleep_s = _next_backoff(attempt)
        time.sleep(sleep_s)
        attempt += 1


def remove_taint_forever(taint_key: str) -> None:
    """Remove `taint_key` from this node. Retries forever on transient errors.

    Exits the function (returns) once the taint is gone (or was never present).
    Raises RuntimeError on permanent errors (HTTP 4xx other than 409).
    """
    token = _read_token()
    ctx = _ssl_context()

    attempt = 0
    while True:
        try:
            _wait_for_instance_type_taint(token, ctx)

            node = _get_node(token, ctx)
            index = _find_taint_index(node, taint_key)
            if index is None:
                log.info("Taint '%s' already absent on node — nothing to do.", taint_key)
                return

            status, body = _patch_remove_taint(token, ctx, index, taint_key)
            if status == 200:
                log.info("Removed taint '%s' from node.", taint_key)
                return
            if status == 409:
                log.info("Taint set changed concurrently (HTTP 409) — re-reading and retrying.")
                attempt = 0
                continue
            if 500 <= status < 600:
                log.warning("PATCH returned HTTP %s: %s — retrying.", status, body)
            elif 400 <= status < 500:
                raise PermanentApiError(f"Permanent error from K8s API (HTTP {status}): {body!r}")
            else:
                log.warning("Unexpected HTTP %s from PATCH: %s — retrying.", status, body)
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            log.warning("Transient network error: %s — retrying.", e)
        except TransientApiError as e:
            log.warning("Transient API error: %s — retrying.", e)
        except PermanentApiError:
            raise
        except Exception:
            log.exception("Unexpected exception in remove_taint_forever — re-raising.")
            raise

        sleep_s = _next_backoff(attempt)
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
