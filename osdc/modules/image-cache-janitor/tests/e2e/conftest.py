"""Session fixtures and setup/teardown for image-cache-janitor e2e tests."""

from __future__ import annotations

import atexit
import logging
import os
import signal
import sys
import tempfile
import time
from contextlib import suppress

import pytest
from helpers import (
    JANITOR_DAEMONSET,
    JANITOR_NAMESPACE,
    KARPENTER_NODEPOOL_LABEL,
    create_test_pod,
    delete_all_pods,
    get_janitor_logs,
    get_janitor_pod_on_node,
    patch_janitor_env,
    restore_janitor_env,
    wait_for,
    wait_for_janitor_pod,
    wait_for_janitor_rollout,
)
from lightkube import Client
from lightkube.generic_resource import create_global_resource
from lightkube.models.meta_v1 import ObjectMeta
from lightkube.resources.core_v1 import Namespace
from lightkube.resources.core_v1 import Pod as PodResource

# ── Logging ──────────────────────────────────────────────────────────────────

LOG_FILE = os.path.join(tempfile.gettempdir(), "janitor-e2e.log")
TEST_NAMESPACE = "janitor-e2e-test"

log = logging.getLogger("e2e")
log.setLevel(logging.DEBUG)
_handler = logging.FileHandler(LOG_FILE, mode="w")
_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-5s %(message)s", datefmt="%H:%M:%S"))
log.addHandler(_handler)
log.propagate = False

# ── Karpenter CRD ───────────────────────────────────────────────────────────

NodePool = create_global_resource(
    group="karpenter.sh",
    version="v1",
    kind="NodePool",
    plural="nodepools",
)

# ── Config dicts ─────────────────────────────────────────────────────────────
# Initial config: high limit (no eviction), fast cycle (10s).

INITIAL_CONFIG = {
    "IMAGE_CACHE_LIMIT_GI": "9999",
    "IMAGE_CACHE_TARGET_GI": "9998",
    "CHECK_INTERVAL_SECONDS": "10",
}

EVICTION_CONFIG = {
    "IMAGE_CACHE_LIMIT_GI": "0",
    "IMAGE_CACHE_TARGET_GI": "0",
    "CHECK_INTERVAL_SECONDS": "10",
}

_RESTORE_CMD = (
    "kubectl -n kube-system set env daemonset/image-cache-janitor"
    " IMAGE_CACHE_LIMIT_GI=500 IMAGE_CACHE_TARGET_GI=400"
    " CHECK_INTERVAL_SECONDS=300"
)


# ── Pytest hooks ─────────────────────────────────────────────────────────────


def pytest_addoption(parser: pytest.Parser) -> None:
    with suppress(ValueError):
        parser.addoption(
            "--cluster-id",
            action="store",
            required=True,
            help="Cluster ID from clusters.yaml (e.g. arc-staging)",
        )


_log_stash_key = pytest.StashKey[str]()


@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item,
    call: pytest.CallInfo,
) -> None:
    outcome = yield
    report = outcome.get_result()
    if report.when == "call" and report.failed:
        pod = item.session.stash.get(_log_stash_key, None)
        if pod:
            lines = get_janitor_logs(pod)
            with open(LOG_FILE, "a") as fh:
                fh.write(f"\n{'=' * 60}\n")
                fh.write(f"Janitor logs for failed test: {item.name}\n")
                fh.write(f"{'=' * 60}\n")
                fh.write("\n".join(lines[-200:]))
                fh.write("\n")
        print(f"\n>>> Janitor e2e logs saved to: {LOG_FILE}")


def pytest_sessionfinish(
    session: pytest.Session,
    exitstatus: int,
) -> None:
    if exitstatus != 0:
        print(f"\n>>> Janitor e2e logs: {LOG_FILE}")


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def cluster_id(request: pytest.FixtureRequest) -> str:
    return request.config.getoption("--cluster-id")


@pytest.fixture(scope="session")
def client() -> Client:
    return Client()


@pytest.fixture(scope="session")
def target_nodepool(client: Client) -> tuple[str, str]:
    """Find a runner/buildkit NodePool. Returns ``(pool_name, instance_type)``."""
    from lightkube.resources.core_v1 import Node

    # Fast path: check existing nodes
    for workload_type in ("github-runner", "buildkit"):
        for node in client.list(Node, labels={"workload-type": workload_type}):
            if node.metadata.deletionTimestamp:
                continue
            labels = node.metadata.labels or {}
            pool = labels.get(KARPENTER_NODEPOOL_LABEL)
            if pool:
                itype = labels.get("node.kubernetes.io/instance-type", "unknown")
                log.info(
                    "Found pool %s (%s) via existing %s node %s",
                    pool,
                    itype,
                    workload_type,
                    node.metadata.name,
                )
                return pool, itype

    # Slow path: inspect NodePool specs
    for np in client.list(NodePool):
        template_labels = np.get("spec", {}).get("template", {}).get("metadata", {}).get("labels", {})
        wt = template_labels.get("workload-type", "")
        if wt in ("github-runner", "buildkit"):
            name = np.metadata.name
            reqs = np.get("spec", {}).get("template", {}).get("spec", {}).get("requirements", [])
            itype = _extract_instance_type(reqs, name)
            log.info("Found pool %s via NodePool spec", name)
            return name, itype

    pytest.skip("No runner/buildkit NodePool found")


def _extract_instance_type(requirements: list[dict], nodepool_name: str) -> str:
    for req in requirements:
        if req.get("key") == "node.kubernetes.io/instance-type":
            values = req.get("values", [])
            if values:
                return values[0]
    # Fallback: derive from nodepool name (e.g. "r5-24xlarge" -> "r5.24xlarge")
    if "-" in nodepool_name:
        return nodepool_name.replace("-", ".", 1)
    return "unknown"


@pytest.fixture(scope="session")
def test_namespace(client: Client):
    """Create and tear down the test namespace."""
    ns = Namespace(metadata=ObjectMeta(name=TEST_NAMESPACE))
    with suppress(Exception):
        client.create(ns)
    # Clean stale pods from a possible crashed previous run
    delete_all_pods(client, TEST_NAMESPACE)
    log.info("Created namespace %s", TEST_NAMESPACE)
    yield TEST_NAMESPACE
    delete_all_pods(client, TEST_NAMESPACE)
    with suppress(Exception):
        client.delete(Namespace, TEST_NAMESPACE)
    log.info("Deleted namespace %s", TEST_NAMESPACE)


@pytest.fixture(scope="session")
def target_node(
    client: Client,
    target_nodepool: tuple[str, str],
    test_namespace: str,
) -> str:
    """Ensure a runner node exists (provision if needed). Returns node name."""
    pool, itype = target_nodepool

    # Create a keepalive pod — ensures a node exists and stays alive
    pod_name = f"janitor-e2e-{int(time.time())}"
    create_test_pod(client, pod_name, test_namespace, pool, itype)

    def _running() -> bool:
        try:
            pod = client.get(PodResource, pod_name, namespace=test_namespace)
            return bool(pod.status and pod.status.phase == "Running")
        except Exception:
            return False

    wait_for("keepalive pod running", _running, timeout_s=600, poll_s=10)

    pod = client.get(PodResource, pod_name, namespace=test_namespace)
    node_name = pod.spec.nodeName
    log.info("Target node: %s (pool=%s, type=%s)", node_name, pool, itype)
    return node_name


def _env_already_matches(client: Client, config: dict[str, str]) -> bool:
    """Check if the DaemonSet env already matches *config* (crashed run)."""
    from lightkube.resources.apps_v1 import DaemonSet

    ds = client.get(DaemonSet, JANITOR_DAEMONSET, namespace=JANITOR_NAMESPACE)
    container = ds.spec.template.spec.containers[0]
    current = {e.name: e.value for e in (container.env or [])}
    return all(current.get(k) == v for k, v in config.items())


@pytest.fixture(scope="session", autouse=True)
def janitor_setup(
    client: Client,
    target_node: str,
    target_nodepool: tuple[str, str],
    request: pytest.FixtureRequest,
):
    """Patch janitor DaemonSet for fast test cycles, restore on teardown."""
    # Wait for janitor pod to be running on the target node
    initial_pod = wait_for_janitor_pod(client, target_node, timeout_s=300)
    log.info("Initial janitor pod: %s", initial_pod)

    # Check BEFORE patching — detect crashed previous run that left test config
    already_configured = _env_already_matches(client, INITIAL_CONFIG)

    # Patch to initial test config (high limit, fast interval)
    production_originals = patch_janitor_env(client, INITIAL_CONFIG)

    # ── Teardown (register IMMEDIATELY after patching) ───────────────────
    # Must be registered before any operation that can fail, so that
    # production values are always restored even on setup timeout/crash.
    restored = False

    def restore() -> None:
        nonlocal restored
        if restored:
            return
        log.info("Restoring janitor production config")
        for attempt in range(3):
            try:
                restore_janitor_env(client, production_originals)
                restored = True
                return
            except Exception:
                log.exception("Restore attempt %d/3 failed", attempt + 1)
                if attempt < 2:
                    time.sleep(5)
        # All retries exhausted — print manual recovery command
        restored = True  # prevent further attempts
        msg = (
            f"\n{'!' * 60}\n"
            f"CRITICAL: Failed to restore janitor DaemonSet config.\n"
            f"The janitor may be running with test values (limit=0).\n"
            f"Run manually:\n  {_RESTORE_CMD}\n"
            f"{'!' * 60}\n"
        )
        log.critical(msg)
        print(msg, file=sys.stderr)

    atexit.register(restore)
    for sig in (signal.SIGTERM, signal.SIGINT):
        prev = signal.getsignal(sig)

        def handler(
            signum: int,
            frame: object,
            _prev: object = prev,
        ) -> None:
            restore()
            if callable(_prev):
                _prev(signum, frame)
            else:
                sys.exit(128 + signum)

        signal.signal(sig, handler)

    # ── Setup (continued) ────────────────────────────────────────────────

    # Handle no-op patch (previous crashed run already set this config)
    if already_configured:
        new_pod = get_janitor_pod_on_node(client, target_node) or initial_pod
        log.info("Config already matches (possible crashed run) — skipping rollout")
    else:
        new_pod = wait_for_janitor_rollout(client, target_node, initial_pod, timeout_s=180)
    log.info("Janitor pod after config patch: %s", new_pod)

    # Wait for first GC cycle (janitor logs "Image cache:" each cycle)
    wait_for(
        "first GC cycle",
        lambda: any("Image cache:" in line for line in get_janitor_logs(new_pod)),
        timeout_s=60,
        poll_s=5,
    )
    log.info("Janitor is cycling — ready for tests")

    # Stash pod name for the log-dump hook
    request.session.stash[_log_stash_key] = new_pod

    yield

    restore()
