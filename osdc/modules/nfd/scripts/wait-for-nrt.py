#!/usr/bin/env python3
"""Wait for NFD to publish a NodeResourceTopology object for this node,
then remove the node-init.osdc.io/nfd-topology startup taint.

Polls the Kubernetes API for an NRT object matching NODE_NAME. Once found,
delegates to the shared taint_remover.py library to remove the taint.

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

# The shared taint-remover library is mounted at /scripts/taint-remover/
sys.path.insert(0, "/scripts/taint-remover")

from taint_remover import (  # noqa: E402
    PermanentApiError,
    TransientApiError,
    _k8s_api,
    _read_token,
    _request,
    _ssl_context,
    remove_taint_forever,
)

TAINT_KEY = "node-init.osdc.io/nfd-topology"
POLL_INTERVAL = 5  # seconds between NRT checks
NRT_API_VERSION = "v1alpha2"
NRT_API_PATH = f"/apis/topology.node.k8s.io/{NRT_API_VERSION}/noderesourcetopologies"

log = logging.getLogger("wait-for-nrt")


def _wait_for_nrt(node_name: str) -> None:
    """Poll until an NRT object exists for this node."""
    url = f"{_k8s_api()}{NRT_API_PATH}/{node_name}"
    ctx = _ssl_context()

    log.info("Waiting for NodeResourceTopology object for node '%s'...", node_name)
    while True:
        token = _read_token()
        try:
            status, _ = _request("GET", url, token, ctx)
            if status == 200:
                log.info("NRT object found for node '%s'.", node_name)
                return
            if status == 404:
                log.info("NRT not yet published for '%s' — retrying in %ds.", node_name, POLL_INTERVAL)
            elif 500 <= status < 600 or status == 429:
                log.warning("Transient error (HTTP %d) checking NRT — retrying.", status)
            else:
                log.warning("Unexpected HTTP %d checking NRT — retrying.", status)
        except (TransientApiError, OSError, TimeoutError, ConnectionError) as e:
            log.warning("Transient error checking NRT: %s — retrying.", e)
        except PermanentApiError as e:
            log.error("Permanent error checking NRT: %s — retrying anyway (CRD may not be registered yet).", e)

        time.sleep(POLL_INTERVAL)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    node_name = os.environ.get("NODE_NAME")
    if not node_name:
        log.error("NODE_NAME env var not set (Downward API spec.nodeName)")
        return 1

    try:
        _wait_for_nrt(node_name)
        remove_taint_forever(TAINT_KEY)
        return 0
    except (PermanentApiError, RuntimeError) as e:
        log.error("%s", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
