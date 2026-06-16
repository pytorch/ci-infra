#!/usr/bin/env python3
"""Wait for NFD to publish a NodeResourceTopology object for this node,
then remove the node-init.osdc.io/nfd-topology startup taint.

Polls the Kubernetes API for an NRT object matching NODE_NAME. Once found,
delegates to the shared taint_remover.py library to remove the taint.

Fails open after TIMEOUT_SECONDS: if NFD hasn't published by then, the
taint is removed anyway to prevent permanently stranding the node.

Usage: wait-for-nrt.py

Environment:
  NODE_NAME (required, from Downward API)
  KUBERNETES_SERVICE_HOST / _PORT (set automatically inside cluster)
"""

from __future__ import annotations

import logging
import os
import sys
import time
import urllib.error

# The shared taint-remover library is mounted at /scripts/taint-remover/. We
# reuse its in-cluster API plumbing (URL building, SA token, TLS context, and
# the HTTP request helper) so this script only owns the NRT-specific polling.
sys.path.insert(0, "/scripts/taint-remover")

from taint_remover import _k8s_api, _read_token, _request, _ssl_context, remove_taint_forever

TAINT_KEY = "node-init.osdc.io/nfd-topology"
POLL_INTERVAL = 5  # seconds between NRT checks
TIMEOUT_SECONDS = 600  # 10 minutes — fail open if exceeded
NRT_API_VERSION = "v1alpha2"  # storage version for NFD 0.17.x
NRT_API_PATH = f"/apis/topology.node.k8s.io/{NRT_API_VERSION}/noderesourcetopologies"

log = logging.getLogger("wait-for-nrt")


def _get_nrt(node_name: str) -> int:
    """GET the NRT object for this node. Returns the HTTP status code.

    Transport errors (timeouts, connection resets) propagate to the caller,
    which treats them as transient and retries. HTTP error responses come
    back as their status code via the shared _request helper.
    """
    url = f"{_k8s_api()}{NRT_API_PATH}/{node_name}"
    status, _ = _request("GET", url, _read_token(), _ssl_context())
    return status


def _wait_for_nrt(node_name: str) -> None:
    """Poll until an NRT object exists for this node, or the timeout elapses.

    Logs the outcome (found, or fail-open on timeout) and returns. The caller
    removes the taint unconditionally — failing open keeps a slow or broken
    NFD from permanently stranding the node.
    """
    deadline = time.monotonic() + TIMEOUT_SECONDS

    log.info(
        "Waiting for NodeResourceTopology object for node '%s' (timeout %ds)...",
        node_name,
        TIMEOUT_SECONDS,
    )
    while time.monotonic() < deadline:
        try:
            status = _get_nrt(node_name)
            if status == 200:
                log.info("NRT object found for node '%s'.", node_name)
                return
            if status == 404:
                log.info("NRT not yet published for '%s' — retrying in %ds.", node_name, POLL_INTERVAL)
            elif 500 <= status < 600 or status == 429:
                log.warning("Transient error (HTTP %d) checking NRT — retrying.", status)
            else:
                log.warning("Unexpected HTTP %d checking NRT — retrying.", status)
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            log.warning("Transient error checking NRT: %s — retrying.", e)

        time.sleep(POLL_INTERVAL)

    log.warning(
        "Timeout (%ds) waiting for NRT on node '%s' — removing taint anyway (fail-open).",
        TIMEOUT_SECONDS,
        node_name,
    )


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    node_name = os.environ.get("NODE_NAME")
    if not node_name:
        log.error("NODE_NAME env var not set (Downward API spec.nodeName)")
        return 1

    _wait_for_nrt(node_name)
    remove_taint_forever(TAINT_KEY)
    return 0


if __name__ == "__main__":
    sys.exit(main())
