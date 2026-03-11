"""Pytest configuration and session-scoped fixtures for node-compactor e2e."""

from __future__ import annotations

import atexit
import contextlib
import logging
import signal
import sys

import pytest
from helpers import (
    COMPACTOR_NODEPOOL_LABEL,
    delete_all_pods,
    delete_pool_nodes,
    drain_pool_workloads,
    get_pool_nodes,
    patch_compactor_env,
    restart_compactor_pod,
    restore_compactor_env,
    wait_for,
)
from lightkube import Client
from lightkube.generic_resource import create_global_resource
from lightkube.models.meta_v1 import ObjectMeta
from lightkube.resources.core_v1 import Namespace


log = logging.getLogger("e2e")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(message)s",
    datefmt="%H:%M:%S",
)

TEST_NAMESPACE = "compactor-e2e-test"

# Karpenter NodePool CRD
NodePool = create_global_resource(
    group="karpenter.sh",
    version="v1",
    kind="NodePool",
    plural="nodepools",
)


# ---------------------------------------------------------------------------
# CLI option
# ---------------------------------------------------------------------------


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--cluster-id",
        action="store",
        required=True,
        help="Cluster ID from clusters.yaml (e.g. arc-staging)",
    )


# ---------------------------------------------------------------------------
# Session fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def cluster_id(request: pytest.FixtureRequest) -> str:
    return request.config.getoption("--cluster-id")


@pytest.fixture(scope="session")
def client() -> Client:
    """lightkube client using current kubeconfig context."""
    return Client()


@pytest.fixture(scope="session")
def target_nodepool(client: Client) -> tuple[str, str]:
    """Discover a compactor-managed NodePool. Returns (name, instance_type).

    Prefer r5-24xlarge (cheapest CPU pool).
    """
    # Collect managed NodePools with their instance types
    managed: list[tuple[str, str]] = []
    for np in client.list(NodePool):
        labels = {}
        if np.get("metadata", {}).get("labels"):
            labels = np["metadata"]["labels"]
        if labels.get(COMPACTOR_NODEPOOL_LABEL) != "true":
            continue
        name = np["metadata"]["name"]
        # Extract instance type from NodePool spec
        itype = ""
        reqs = (
            np.get("spec", {})
            .get("template", {})
            .get("spec", {})
            .get("requirements", [])
        )
        for req in reqs:
            if req.get("key") == "node.kubernetes.io/instance-type":
                values = req.get("values", [])
                if values:
                    itype = values[0]
                break
        managed.append((name, itype))

    if not managed:
        pytest.skip("No compactor-managed NodePools found in cluster")

    # Prefer r5-24xlarge (cheapest CPU pool)
    for name, itype in managed:
        if name == "r5-24xlarge":
            return (name, itype)
    return managed[0]


@pytest.fixture(scope="session")
def target_nodepool_name(target_nodepool: tuple[str, str]) -> str:
    """NodePool name extracted from target_nodepool tuple."""
    return target_nodepool[0]


@pytest.fixture(scope="session")
def instance_type(target_nodepool: tuple[str, str]) -> str:
    """Instance type extracted from NodePool spec (e.g. r5.24xlarge)."""
    name, itype = target_nodepool
    if itype:
        return itype
    # Fallback: derive from nodepool name (last hyphen -> dot)
    parts = name.rsplit("-", 1)
    if len(parts) == 2:
        return f"{parts[0]}.{parts[1]}"
    return name


@pytest.fixture(scope="session")
def test_namespace(client: Client) -> str:
    """Create the test namespace and clean up after."""
    ns = Namespace(metadata=ObjectMeta(name=TEST_NAMESPACE))
    # Might already exist from a failed previous run
    with contextlib.suppress(Exception):
        client.create(ns)

    # Clean up stale pods from a failed previous run
    delete_all_pods(client, TEST_NAMESPACE)

    yield TEST_NAMESPACE

    # Cleanup
    with contextlib.suppress(Exception):
        delete_all_pods(client, TEST_NAMESPACE)
    with contextlib.suppress(Exception):
        client.delete(Namespace, name=TEST_NAMESPACE)


@pytest.fixture(scope="session", autouse=True)
def compactor_setup(
    client: Client,
    target_nodepool_name: str,
    test_namespace: str,
) -> None:
    """Patch compactor for fast testing cycles, restore on exit."""
    # Override env vars for fast iteration
    test_overrides = {
        "COMPACTOR_DRY_RUN": "false",
        "COMPACTOR_INTERVAL": "10",
        "COMPACTOR_TAINT_COOLDOWN": "30",
    }

    log.info("Patching compactor Deployment for e2e testing...")
    originals = patch_compactor_env(client, test_overrides)
    log.info("  Original env: %s", originals)
    log.info("  Test overrides: %s", test_overrides)

    # Restart compactor pod to pick up new env
    log.info("Restarting compactor pod...")
    restart_compactor_pod(client)

    # Best-effort: drain existing workload pods from target pool
    log.info("Draining existing workloads from pool %s...", target_nodepool_name)
    drain_pool_workloads(client, target_nodepool_name, TEST_NAMESPACE)

    # Forcefully delete stale nodes so tests start with a clean pool.
    # Karpenter will re-provision fresh nodes when test pods are created.
    pool_nodes = get_pool_nodes(client, target_nodepool_name)
    if pool_nodes:
        log.info("Deleting %d stale pool nodes...", len(pool_nodes))
        delete_pool_nodes(client, target_nodepool_name)
        wait_for(
            "pool nodes to be deleted",
            lambda: len(get_pool_nodes(client, target_nodepool_name)) == 0,
            timeout_s=300,
            poll_s=10,
        )
        log.info("Pool is empty — ready for tests.")

    # ----- Cleanup on exit (always) -----

    restored = False

    def restore() -> None:
        nonlocal restored
        if restored:
            return
        restored = True
        log.info("Restoring compactor Deployment env vars...")
        try:
            restore_compactor_env(client, originals)
            restart_compactor_pod(client)
            log.info("  Compactor restored.")
        except Exception:
            log.exception("  Failed to restore compactor env!")

    # Register cleanup via multiple mechanisms for robustness
    atexit.register(restore)

    def signal_handler(signum: int, frame: object) -> None:
        log.info("Caught signal %d, cleaning up...", signum)
        restore()
        sys.exit(128 + signum)

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    yield  # type: ignore[misc]

    # pytest finalizer path
    restore()
    atexit.unregister(restore)
