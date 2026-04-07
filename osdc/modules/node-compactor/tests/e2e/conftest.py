"""Pytest configuration and session-scoped fixtures for node-compactor e2e.

-----------------------------------------------------------------------
Concurrency note: smoke test interaction

These e2e tests mutate cluster state that smoke tests also verify.
Smoke tests (just smoke) are designed to tolerate this via
assert_daemonset_healthy() which ignores nodes in transition.

State mutated by these e2e tests:
  - Karpenter nodes in target NodePool: deleted at setup, provisioned
    on demand during tests, may be deleted by Karpenter's WhenEmpty
  - node-compactor Deployment (kube-system): env vars patched for fast
    cycles, pod restarted; restored at teardown
  - compactor-e2e-test namespace: created at setup, deleted at teardown
  - Node taints: node-compactor.osdc.io/consolidating applied/removed

If you change what these tests mutate, evaluate whether the smoke
tests' resilience mechanisms (assert_daemonset_healthy, Deployment
retry window) still cover the new mutations.
-----------------------------------------------------------------------
"""

from __future__ import annotations

import atexit
import contextlib
import logging
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time

import pytest
from helpers import (
    COMPACTOR_DEPLOYMENT,
    COMPACTOR_NAMESPACE,
    COMPACTOR_NODEPOOL_LABEL,
    cleanup_stale_cluster_state,
    delete_all_pods,
    delete_pool_nodes,
    drain_pool_workloads,
    get_compactor_pod_names,
    get_pool_nodes,
    patch_compactor_env,
    restart_compactor_pod,
    restore_compactor_env,
    scale_compactor_deployment,
    wait_for,
    wait_for_compactor_rollout,
)
from lightkube import Client
from lightkube.generic_resource import create_global_resource
from lightkube.models.meta_v1 import ObjectMeta
from lightkube.resources.apps_v1 import Deployment as DeploymentResource
from lightkube.resources.core_v1 import Namespace

LOG_FILE = os.path.join(tempfile.gettempdir(), "compactor-e2e.log")

# Configure e2e logger to write to file only (not stdout)
_log_handler = logging.FileHandler(LOG_FILE, mode="w")
_log_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-5s %(message)s", datefmt="%H:%M:%S"))
log = logging.getLogger("e2e")
log.setLevel(logging.INFO)
log.addHandler(_log_handler)
log.propagate = False  # prevent duplicate output to root logger / pytest

TEST_NAMESPACE = "compactor-e2e-test"

# ---------------------------------------------------------------------------
# Per-group compactor configurations
# ---------------------------------------------------------------------------

# Group A: bare compactor — no anti-flap, no reservation
GROUP_A_CONFIG = {
    "COMPACTOR_DRY_RUN": "false",
    "COMPACTOR_INTERVAL": "5",
    "COMPACTOR_TAINT_COOLDOWN": "30",
    "COMPACTOR_MIN_NODE_AGE": "0",
    "COMPACTOR_TAINT_RATE": "1.0",
    "COMPACTOR_FLEET_COOLDOWN": "0",
    "COMPACTOR_SPARE_CAPACITY_NODES": "0",
    "COMPACTOR_SPARE_CAPACITY_RATIO": "0",
    "COMPACTOR_SPARE_CAPACITY_THRESHOLD": "0.4",
    "COMPACTOR_CAPACITY_RESERVATION_NODES": "0",
}

# Group B: anti-flap mechanisms — min_node_age blocks first, then rate + cooldown
GROUP_B_MIN_AGE_CONFIG = {
    **GROUP_A_CONFIG,
    "COMPACTOR_MIN_NODE_AGE": "3600",
}

GROUP_B_RATE_COOLDOWN_CONFIG = {
    **GROUP_A_CONFIG,
    "COMPACTOR_MIN_NODE_AGE": "0",
    "COMPACTOR_TAINT_RATE": "0.25",
    "COMPACTOR_FLEET_COOLDOWN": "90",
}

# Group C: reservation behaviour
GROUP_C_CONFIG = {
    **GROUP_A_CONFIG,
    "COMPACTOR_CAPACITY_RESERVATION_NODES": "1",
}


# ---------------------------------------------------------------------------
# Compactor log capture
# ---------------------------------------------------------------------------


class CompactorLogCollector:
    """Streams compactor pod logs in a background thread.

    Captures output into a list of lines. On test failure the collected
    logs are appended to the e2e log file for diagnostics.
    """

    def __init__(self) -> None:
        self._lines: list[str] = []
        self._proc: subprocess.Popen[str] | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    @property
    def lines(self) -> list[str]:
        return list(self._lines)

    def start(self) -> None:
        """Start streaming compactor logs in a background thread."""
        self._stop.clear()
        self._thread = threading.Thread(target=self._stream, daemon=True)
        self._thread.start()

    def _stream(self) -> None:
        """Run kubectl logs -f in a loop, retrying on disconnect."""
        while not self._stop.is_set():
            try:
                self._proc = subprocess.Popen(
                    [
                        "kubectl",
                        "logs",
                        "-f",
                        "-n",
                        COMPACTOR_NAMESPACE,
                        "-l",
                        "app.kubernetes.io/name=node-compactor",
                        "--tail=100",
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    bufsize=1,
                )
                # iter(readline, '') reads line-by-line as each newline
                # arrives. Do NOT use `for line in proc.stdout:` — Python's
                # file iterator uses 8KB block buffering regardless of
                # bufsize=1, delaying log lines until the buffer fills.
                for line in iter(self._proc.stdout.readline, ""):  # type: ignore[union-attr]
                    if self._stop.is_set():
                        break
                    self._lines.append(line.rstrip("\n"))
                self._proc.wait()
            except Exception:
                log.debug("Log stream disconnected, will retry")
            # Brief pause before retry on disconnect / pod restart
            if not self._stop.is_set():
                self._stop.wait(timeout=2)

    def stop(self) -> None:
        """Signal the background thread to stop and wait for it."""
        self._stop.set()
        if self._proc:
            with contextlib.suppress(Exception):
                self._proc.terminate()
                self._proc.wait(timeout=5)
        if self._thread:
            self._thread.join(timeout=10)

    def dump(self) -> None:
        """Append captured compactor pod logs to the e2e log file."""
        with open(LOG_FILE, "a") as f:
            if not self._lines:
                f.write("\n[compactor logs] (no logs captured)\n")
                return
            f.write(f"\n[compactor logs] ({len(self._lines)} lines)\n")
            f.write("-" * 60 + "\n")
            for line in self._lines:
                f.write(line + "\n")
            f.write("-" * 60 + "\n")


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
        reqs = np.get("spec", {}).get("template", {}).get("spec", {}).get("requirements", [])
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


@pytest.fixture(scope="session")
def compactor_logs() -> CompactorLogCollector:
    """Session-scoped log collector — started by compactor_setup after rollout."""
    collector = CompactorLogCollector()
    yield collector
    collector.stop()


@pytest.fixture(scope="session", autouse=True)
def compactor_setup(
    client: Client,
    target_nodepool_name: str,
    test_namespace: str,
    compactor_logs: CompactorLogCollector,
) -> None:
    """Patch compactor for fast testing cycles, restore on exit."""
    # Guard: if a previous run crashed after scaling to 0, restore replicas
    dep = client.get(
        DeploymentResource,
        name=COMPACTOR_DEPLOYMENT,
        namespace=COMPACTOR_NAMESPACE,
    )
    if dep.spec and dep.spec.replicas is not None and dep.spec.replicas == 0:
        log.info("Compactor scaled to 0 (stale from crashed run) — restoring to 1")
        scale_compactor_deployment(client, 1)

    # Clean stale taints and reservation annotations from pool nodes
    # left behind by a crashed previous run.
    log.info("Cleaning stale cluster state from pool %s...", target_nodepool_name)
    cleanup_stale_cluster_state(client, target_nodepool_name)

    # Override env vars for fast iteration
    test_overrides = GROUP_A_CONFIG

    # Capture pre-patch pod names so we can detect the rollout
    old_pod_names = get_compactor_pod_names(client)

    log.info("Patching compactor Deployment for e2e testing...")
    originals = patch_compactor_env(client, test_overrides)
    log.info("  Original env: %s", originals)
    log.info("  Test overrides: %s", test_overrides)

    # Detect no-op patches: if a previous test run crashed without restoring
    # env vars, the originals will already match the overrides. In that case
    # no Deployment rollout occurs (template unchanged), so we skip the
    # rollout wait and reconciliation gate — the pod is already running
    # with the correct config and has been reconciling.
    patch_is_noop = all(str(originals.get(k, "")) == str(v) for k, v in test_overrides.items())

    if patch_is_noop:
        log.info("  Env vars already match test overrides (previous run did not restore).")
        log.info("  Skipping rollout wait — verifying existing pod is healthy...")
        wait_for(
            "compactor pod running",
            lambda: len(get_compactor_pod_names(client)) > 0,
            timeout_s=30,
            poll_s=5,
        )
    else:
        # patch_compactor_env modifies the pod template, which automatically
        # triggers a Deployment rollout (new ReplicaSet + new pod). No need
        # to delete the pod — just wait for the new one.
        log.info("Waiting for compactor rollout after env patch...")
        wait_for_compactor_rollout(client, old_pod_names)

        # Wait for the compactor to complete at least one reconciliation cycle
        # so tests start against a controller that has seen current cluster state.
        log.info("Waiting for compactor to complete first reconciliation...")
        _wait_for_compactor_reconcile()

    # Start log capture AFTER the new pod is running with test config
    log.info("Starting compactor log capture...")
    compactor_logs.start()

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
        compactor_logs.stop()
        log.info("Restoring compactor Deployment (env vars + replicas)...")
        try:
            # Ensure replicas=1 before restoring env (a crashed test may
            # have left the deployment scaled to 0).
            dep = client.get(
                DeploymentResource,
                name=COMPACTOR_DEPLOYMENT,
                namespace=COMPACTOR_NAMESPACE,
            )
            if dep.spec and dep.spec.replicas is not None and dep.spec.replicas == 0:
                log.info("  Replicas=0 detected, scaling back to 1...")
                scale_compactor_deployment(client, 1)
            restore_compactor_env(client, originals)
            restart_compactor_pod(client)
            log.info("  Compactor restored.")
        except Exception:
            log.exception("  Failed to restore compactor!")

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


# ---------------------------------------------------------------------------
# Reconciliation gate
# ---------------------------------------------------------------------------


def _wait_for_compactor_reconcile(timeout_s: int = 60) -> None:
    """Wait until the compactor has completed at least one reconcile cycle.

    Accepts either "Reconciling:" (nodes found) or "Node Compactor starting"
    (pod alive, but 0 managed nodes — nothing to reconcile, tests will
    provision their own nodes).
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            result = subprocess.run(
                [
                    "kubectl",
                    "logs",
                    "-n",
                    COMPACTOR_NAMESPACE,
                    "-l",
                    "app.kubernetes.io/name=node-compactor",
                    "--tail=50",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if "Reconciling:" in result.stdout:
                log.info("  Compactor reconciliation detected.")
                return
            if "Node Compactor starting" in result.stdout:
                log.info("  Compactor running (no managed nodes yet — tests will provision).")
                return
        except Exception:
            log.debug("Failed to read compactor logs, will retry")
        time.sleep(2)
    raise TimeoutError(f"Compactor did not complete a reconciliation cycle within {timeout_s}s")


# ---------------------------------------------------------------------------
# Pytest hook: dump compactor logs on test failure
# ---------------------------------------------------------------------------

# Stash key for per-test failure tracking
_phase_report_key = pytest.StashKey[dict[str, pytest.TestReport]]()


@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item,
    call: pytest.CallInfo[None],
) -> None:
    """Stash test outcome so fixtures can detect failures."""
    outcome = yield
    rep: pytest.TestReport = outcome.get_result()
    item.stash.setdefault(_phase_report_key, {})[rep.when] = rep

    # On test failure: dump compactor logs to file and print pointer
    if rep.when == "call" and rep.failed:
        collector = item.session.stash.get(_compactor_logs_key, None)
        if collector is not None:
            collector.dump()
        print(f"\nDetailed logs at {LOG_FILE}")


# Stash key to share the collector with the hook
_compactor_logs_key = pytest.StashKey[CompactorLogCollector]()


@pytest.fixture(autouse=True)
def _register_log_collector(
    request: pytest.FixtureRequest,
    compactor_logs: CompactorLogCollector,
) -> None:
    """Make the log collector accessible to pytest_runtest_makereport."""
    request.session.stash[_compactor_logs_key] = compactor_logs


def pytest_sessionfinish(
    session: pytest.Session,
    exitstatus: int,
) -> None:
    """Print log file path at end of failed runs."""
    if exitstatus != 0:
        print(f"\nDetailed logs at {LOG_FILE}")
