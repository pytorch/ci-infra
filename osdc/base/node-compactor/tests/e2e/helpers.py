"""Kubernetes helpers for node-compactor e2e tests."""

from __future__ import annotations

import contextlib
import logging
import re
import time
from collections.abc import Callable

from lightkube import Client
from lightkube.models.core_v1 import (
    Container,
    PodSpec,
    ResourceRequirements,
    Toleration,
)
from lightkube.models.meta_v1 import ObjectMeta
from lightkube.resources.apps_v1 import Deployment as DeploymentResource
from lightkube.resources.core_v1 import Node
from lightkube.resources.core_v1 import Pod as PodResource
from lightkube.types import PatchType

log = logging.getLogger("e2e")

# ---------------------------------------------------------------------------
# Constants — keep in sync with scripts/python/models.py
# ---------------------------------------------------------------------------

COMPACTOR_TAINT_KEY = "node-compactor.osdc.io/consolidating"
INSTANCE_TYPE_TAINT_KEY = "instance-type"
COMPACTOR_NODEPOOL_LABEL = "osdc.io/node-compactor"
KARPENTER_NODEPOOL_LABEL = "karpenter.sh/nodepool"
COMPACTOR_DEPLOYMENT = "node-compactor"
COMPACTOR_NAMESPACE = "kube-system"
COMPACTOR_POD_LABEL = "app.kubernetes.io/name=node-compactor"
TEST_IMAGE = "registry.k8s.io/pause:3.10"


# ---------------------------------------------------------------------------
# Polling
# ---------------------------------------------------------------------------


def wait_for(
    description: str,
    check_fn: Callable[[], bool],
    timeout_s: int = 300,
    poll_s: int = 10,
    on_timeout: Callable[[], str] | None = None,
) -> None:
    """Poll ``check_fn`` until it returns True or *timeout_s* expires.

    If *on_timeout* is provided, it is called when the timeout fires and
    its return value is appended to the ``TimeoutError`` message — useful
    for dumping diagnostic state (nodes, pods, taints) on failure.
    """
    deadline = time.monotonic() + timeout_s
    log.info("Waiting for: %s (timeout %ds)", description, timeout_s)
    while True:
        if check_fn():
            log.info("  OK: %s", description)
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            msg = f"Timed out after {timeout_s}s waiting for: {description}"
            if on_timeout:
                msg += f"\nDiagnostics:\n{on_timeout()}"
            raise TimeoutError(msg)
        time.sleep(min(poll_s, remaining))


def wait_for_stable(
    description: str,
    state_fn: Callable[[], object],
    stable_s: float = 20,
    timeout_s: int = 120,
    poll_s: int = 5,
    on_timeout: Callable[[], str] | None = None,
) -> None:
    """Poll ``state_fn`` until its return value is unchanged for *stable_s*.

    Use instead of hard ``time.sleep`` when waiting for a controller to
    stabilise — this returns as soon as the state is genuinely stable
    rather than waiting a fixed duration.

    If *on_timeout* is provided, it is called when the timeout fires and
    its return value is appended to the ``TimeoutError`` message.
    """
    deadline = time.monotonic() + timeout_s
    log.info("Waiting for stable: %s (stable %gs, timeout %ds)", description, stable_s, timeout_s)
    last_state = state_fn()
    stable_since = time.monotonic()
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            elapsed_stable = time.monotonic() - stable_since
            msg = (
                f"Timed out after {timeout_s}s waiting for stable: {description} "
                f"(last change {elapsed_stable:.0f}s ago)"
            )
            if on_timeout:
                msg += f"\nDiagnostics:\n{on_timeout()}"
            raise TimeoutError(msg)
        time.sleep(min(poll_s, remaining))
        current = state_fn()
        if current != last_state:
            last_state = current
            stable_since = time.monotonic()
        elapsed_stable = time.monotonic() - stable_since
        if elapsed_stable >= stable_s:
            log.info("  Stable: %s (unchanged for %gs)", description, elapsed_stable)
            return


def wait_for_pods_deleted(client: Client, namespace: str, names: list[str], timeout_s: int = 60) -> None:
    """Wait for pods to be fully removed from the API (not just Terminating)."""

    def _pods_gone() -> bool:
        existing = {
            p.metadata.name for p in client.list(PodResource, namespace=namespace) if p.metadata and p.metadata.name
        }
        return not existing.intersection(names)

    wait_for(
        f"pods deleted: {names}",
        _pods_gone,
        timeout_s=timeout_s,
        poll_s=2,
    )


# ---------------------------------------------------------------------------
# Node queries
# ---------------------------------------------------------------------------


def get_pool_nodes(client: Client, nodepool_name: str) -> list[Node]:
    """Return all Ready nodes belonging to *nodepool_name*."""
    nodes = []
    for node in client.list(Node, labels={KARPENTER_NODEPOOL_LABEL: nodepool_name}):
        # Only include nodes that exist and are not being deleted
        if node.metadata and not node.metadata.deletionTimestamp:
            nodes.append(node)
    return nodes


def _node_has_taint(node: Node, taint_key: str) -> bool:
    if not node.spec or not node.spec.taints:
        return False
    return any(t.key == taint_key for t in node.spec.taints)


def get_tainted_nodes(client: Client, nodepool_name: str, taint_key: str = COMPACTOR_TAINT_KEY) -> list[str]:
    """Return names of nodes in *nodepool_name* that have the compactor taint."""
    return [
        node.metadata.name
        for node in get_pool_nodes(client, nodepool_name)
        if node.metadata and _node_has_taint(node, taint_key)
    ]


def get_untainted_nodes(client: Client, nodepool_name: str, taint_key: str = COMPACTOR_TAINT_KEY) -> list[str]:
    """Return names of nodes in *nodepool_name* without the compactor taint."""
    return [
        node.metadata.name
        for node in get_pool_nodes(client, nodepool_name)
        if node.metadata and not _node_has_taint(node, taint_key)
    ]


def partition_pool_nodes(
    client: Client, nodepool_name: str, taint_key: str = COMPACTOR_TAINT_KEY
) -> tuple[list[Node], list[str], list[str]]:
    """Single-snapshot partition of pool nodes into tainted/untainted.

    Returns ``(nodes, tainted_names, untainted_names)`` from one API call,
    eliminating TOCTOU races between separate ``get_tainted_nodes`` /
    ``get_untainted_nodes`` calls.
    """
    nodes = get_pool_nodes(client, nodepool_name)
    tainted = [node.metadata.name for node in nodes if node.metadata and _node_has_taint(node, taint_key)]
    untainted = [node.metadata.name for node in nodes if node.metadata and not _node_has_taint(node, taint_key)]
    return nodes, tainted, untainted


def assert_instance_type_taints_preserved(client: Client, nodepool_name: str) -> None:
    """Verify every node with an instance-type label still has its instance-type taint.

    Regression guard: the compactor must never wipe the instance-type taint
    when applying or removing its own taint (the root cause of the
    ImagePullBackOff bug that prompted the RFC 6902 JSON Patch rewrite).
    """
    for node in get_pool_nodes(client, nodepool_name):
        labels = node.metadata.labels or {} if node.metadata else {}
        expected_value = labels.get(INSTANCE_TYPE_TAINT_KEY)
        if not expected_value:
            continue  # node doesn't expect an instance-type taint
        assert _node_has_taint(node, INSTANCE_TYPE_TAINT_KEY), (
            f"Node {node.metadata.name} has instance-type label "
            f"'{expected_value}' but is MISSING the instance-type taint. "
            f"The compactor may have wiped it during a taint operation."
        )


# ---------------------------------------------------------------------------
# Pod helpers
# ---------------------------------------------------------------------------


def get_pods_by_node(client: Client, namespace: str) -> dict[str, list[str]]:
    """Return ``{node_name: [pod_name, ...]}`` for Running pods in *namespace*.

    Pods with a ``deletionTimestamp`` (Terminating) are excluded — they still
    have ``spec.nodeName`` set but are no longer meaningful workloads.
    """
    result: dict[str, list[str]] = {}
    for pod in client.list(PodResource, namespace=namespace):
        # Skip Terminating pods (deletionTimestamp set)
        if pod.metadata and pod.metadata.deletionTimestamp:
            continue
        # Only include Running pods
        if not pod.status or pod.status.phase != "Running":
            continue
        node = pod.spec.nodeName if pod.spec else None
        name = pod.metadata.name if pod.metadata else "unknown"
        if node:
            result.setdefault(node, []).append(name)
    return result


def create_test_pod(
    client: Client,
    name: str,
    namespace: str,
    nodepool: str,
    instance_type: str,
    cpu: str,
    memory: str,
) -> None:
    """Create a test pod targeting *nodepool* with specified resource requests."""
    pod = PodResource(
        metadata=ObjectMeta(
            name=name,
            namespace=namespace,
            labels={"app": "compactor-e2e"},
        ),
        spec=PodSpec(
            restartPolicy="Never",
            terminationGracePeriodSeconds=0,
            nodeSelector={KARPENTER_NODEPOOL_LABEL: nodepool},
            tolerations=[
                Toleration(
                    key="instance-type",
                    operator="Equal",
                    value=instance_type,
                    effect="NoSchedule",
                ),
                Toleration(
                    key="git-cache-not-ready",
                    operator="Exists",
                    effect="NoSchedule",
                ),
            ],
            containers=[
                Container(
                    name="pause",
                    image=TEST_IMAGE,
                    resources=ResourceRequirements(
                        requests={"cpu": cpu, "memory": memory},
                    ),
                )
            ],
        ),
    )
    client.create(pod, namespace=namespace)


def delete_pods(client: Client, namespace: str, names: list[str]) -> None:
    """Delete specific pods by name."""
    for name in names:
        try:
            client.delete(PodResource, name=name, namespace=namespace)
        except Exception:
            log.warning("Failed to delete pod %s/%s", namespace, name)


def delete_all_pods(client: Client, namespace: str) -> None:
    """Delete all pods in *namespace*."""
    for pod in client.list(PodResource, namespace=namespace):
        if pod.metadata and pod.metadata.name:
            with contextlib.suppress(Exception):
                client.delete(PodResource, pod.metadata.name, namespace=namespace)


def all_pods_running(client: Client, namespace: str, count: int) -> bool:
    """Return True when exactly *count* non-Terminating pods are Running.

    Kubernetes reports Terminating pods as ``phase=Running`` until the
    kubelet stops the container.  Filtering on ``deletionTimestamp``
    prevents inflated counts during pod teardown.
    """
    pods = list(client.list(PodResource, namespace=namespace))
    running = [
        p
        for p in pods
        if p.status and p.status.phase == "Running" and not (p.metadata and p.metadata.deletionTimestamp)
    ]
    return len(running) == count


# ---------------------------------------------------------------------------
# Compactor deployment management
# ---------------------------------------------------------------------------


def patch_compactor_env(client: Client, env_overrides: dict[str, str]) -> dict[str, str]:
    """Patch compactor Deployment env vars. Returns original values.

    For env vars that didn't previously exist, the original value is stored
    as the sentinel ``_SENTINEL_MISSING`` so that ``restore_compactor_env``
    can remove them rather than setting an empty string (which would crash
    the compactor on ``int("")``).
    """
    dep = client.get(
        DeploymentResource,
        name=COMPACTOR_DEPLOYMENT,
        namespace=COMPACTOR_NAMESPACE,
    )

    # lightkube Deployment uses typed dataclass objects (dot notation)
    containers = dep.spec.template.spec.containers
    target = None
    for c in containers:
        if c.name == "compactor" or len(containers) == 1:
            target = c
            break
    if not target:
        raise RuntimeError("Could not find compactor container in Deployment")

    env_list = target.env or []
    originals: dict[str, str] = {}

    for key, _new_val in env_overrides.items():
        found = False
        for entry in env_list:
            if entry.name == key:
                originals[key] = entry.value or ""
                found = True
                break
        if not found:
            # Mark as not originally present so restore can remove it
            originals[key] = _SENTINEL_MISSING

    # Build the patch env list: apply overrides to existing, add new ones
    patch_env = []
    for entry in env_list:
        if entry.name in env_overrides:
            patch_env.append({"name": entry.name, "value": str(env_overrides[entry.name])})
        else:
            patch_env.append({"name": entry.name, "value": entry.value or ""})
    # Add env vars that weren't in the original
    existing_names = {e.name for e in env_list}
    for key, val in env_overrides.items():
        if key not in existing_names:
            patch_env.append({"name": key, "value": str(val)})

    # Strategic merge patch with raw dict (lightkube convention)
    patch_body = {
        "spec": {
            "template": {
                "spec": {
                    "containers": [{"name": target.name, "env": patch_env}],
                }
            }
        }
    }
    client.patch(
        DeploymentResource,
        name=COMPACTOR_DEPLOYMENT,
        namespace=COMPACTOR_NAMESPACE,
        obj=patch_body,
    )
    return originals


# Sentinel for env vars that didn't exist before patching
_SENTINEL_MISSING = "__E2E_MISSING__"


def restore_compactor_env(client: Client, originals: dict[str, str]) -> None:
    """Restore compactor Deployment env vars to their original values.

    Env vars marked with ``_SENTINEL_MISSING`` are removed entirely.
    """
    dep = client.get(
        DeploymentResource,
        name=COMPACTOR_DEPLOYMENT,
        namespace=COMPACTOR_NAMESPACE,
    )

    containers = dep.spec.template.spec.containers
    target = None
    for c in containers:
        if c.name == "compactor" or len(containers) == 1:
            target = c
            break
    if not target:
        raise RuntimeError("Could not find compactor container in Deployment")

    env_list = target.env or []
    # Keys to remove (they were added by patching, not originally present)
    keys_to_remove = {k for k, v in originals.items() if v == _SENTINEL_MISSING}
    # Keys to restore to original values
    keys_to_restore = {k: v for k, v in originals.items() if v != _SENTINEL_MISSING}

    patch_env = []
    for entry in env_list:
        if entry.name in keys_to_remove:
            continue  # drop it
        if entry.name in keys_to_restore:
            patch_env.append({"name": entry.name, "value": keys_to_restore[entry.name]})
        else:
            patch_env.append({"name": entry.name, "value": entry.value or ""})

    patch_body = {
        "spec": {
            "template": {
                "spec": {
                    "containers": [{"name": target.name, "env": patch_env}],
                }
            }
        }
    }
    client.patch(
        DeploymentResource,
        name=COMPACTOR_DEPLOYMENT,
        namespace=COMPACTOR_NAMESPACE,
        obj=patch_body,
    )


def restart_compactor_pod(client: Client) -> None:
    """Delete the compactor pod and wait for a *new* pod to be Running.

    The old pod stays in ``Running`` phase during its graceful termination
    period, so we must track old pod names and wait for them to disappear
    before checking for a replacement.
    """
    old_names: set[str] = set()
    for pod in client.list(
        PodResource,
        namespace=COMPACTOR_NAMESPACE,
        labels={"app.kubernetes.io/name": "node-compactor"},
    ):
        if pod.metadata and pod.metadata.name:
            old_names.add(pod.metadata.name)
            client.delete(
                PodResource,
                pod.metadata.name,
                namespace=COMPACTOR_NAMESPACE,
            )

    # Wait for old pod(s) to fully terminate
    def _old_pods_gone() -> bool:
        for pod in client.list(
            PodResource,
            namespace=COMPACTOR_NAMESPACE,
            labels={"app.kubernetes.io/name": "node-compactor"},
        ):
            if pod.metadata and pod.metadata.name in old_names:
                return False
        return True

    if old_names:
        wait_for(
            f"old compactor pod(s) terminated: {old_names}",
            _old_pods_gone,
            timeout_s=120,
            poll_s=2,
        )

    # Wait for a new pod to be Running
    wait_for(
        "new compactor pod running",
        lambda: _compactor_pod_running(client),
        timeout_s=120,
        poll_s=5,
    )


def get_compactor_pod_names(client: Client) -> set[str]:
    """Return names of all non-terminating compactor pods."""
    names: set[str] = set()
    for pod in client.list(
        PodResource,
        namespace=COMPACTOR_NAMESPACE,
        labels={"app.kubernetes.io/name": "node-compactor"},
    ):
        if pod.metadata and pod.metadata.name and not pod.metadata.deletionTimestamp:
            names.add(pod.metadata.name)
    return names


def wait_for_compactor_rollout(client: Client, old_pod_names: set[str], timeout_s: int = 200) -> None:
    """Wait for the Deployment rollout to replace old pods with a new Running pod.

    After ``patch_compactor_env`` modifies the pod template, Kubernetes
    automatically creates a new ReplicaSet and pod.  This waits for a
    Running pod whose name is NOT in *old_pod_names*.

    The default timeout (200s) must exceed ``terminationGracePeriodSeconds``
    (120s) because the Recreate strategy requires the old pod to fully
    terminate before the new pod can start.
    """

    def _new_pod_running() -> bool:
        for pod in client.list(
            PodResource,
            namespace=COMPACTOR_NAMESPACE,
            labels={"app.kubernetes.io/name": "node-compactor"},
        ):
            if not pod.metadata or not pod.metadata.name:
                continue
            if pod.metadata.deletionTimestamp:
                continue
            if pod.metadata.name in old_pod_names:
                continue
            if pod.status and pod.status.phase == "Running":
                return True
        return False

    wait_for(
        "new compactor pod running after rollout",
        _new_pod_running,
        timeout_s=timeout_s,
        poll_s=5,
    )


def scale_compactor_deployment(client: Client, replicas: int) -> None:
    """Scale the compactor Deployment to *replicas* and wait for rollout.

    Setting replicas=0 triggers SIGTERM on existing pods (graceful shutdown
    handler runs) without immediately starting a replacement — useful for
    testing cleanup behaviour in isolation.
    """
    patch_body = {"spec": {"replicas": replicas}}
    client.patch(
        DeploymentResource,
        name=COMPACTOR_DEPLOYMENT,
        namespace=COMPACTOR_NAMESPACE,
        obj=patch_body,
    )
    if replicas == 0:
        # Wait for all compactor pods to terminate
        wait_for(
            "compactor scaled to 0",
            lambda: not _compactor_pod_running(client),
            timeout_s=120,
            poll_s=2,
        )
    else:
        # Wait for at least one pod to be Running
        wait_for(
            f"compactor scaled to {replicas}",
            lambda: _compactor_pod_running(client),
            timeout_s=120,
            poll_s=5,
        )


def _compactor_pod_running(client: Client) -> bool:
    """Check if a non-terminating compactor pod is Running."""
    for pod in client.list(
        PodResource,
        namespace=COMPACTOR_NAMESPACE,
        labels={"app.kubernetes.io/name": "node-compactor"},
    ):
        if pod.status and pod.status.phase == "Running" and not (pod.metadata and pod.metadata.deletionTimestamp):
            return True
    return False


def _no_compactor_pods(client: Client) -> bool:
    """Check that no compactor pods exist at all (including terminating ones).

    Unlike _compactor_pod_running, this returns True only when pods are
    fully removed from the API — not just when they have a deletionTimestamp.
    Use this to wait for shutdown cleanup to complete before verifying results.
    """
    pods = list(
        client.list(
            PodResource,
            namespace=COMPACTOR_NAMESPACE,
            labels={"app.kubernetes.io/name": "node-compactor"},
        )
    )
    return len(pods) == 0


def wait_for_compactor_fully_terminated(client: Client, timeout_s: int = 180) -> None:
    """Wait until all compactor pods are completely gone.

    scale_compactor_deployment(client, 0) returns as soon as pods have a
    deletionTimestamp, but the container may still be running its shutdown
    handler (taint + reservation cleanup). This waits for the pod to be
    fully deleted from the API server.
    """
    wait_for(
        "compactor pod fully terminated",
        lambda: _no_compactor_pods(client),
        timeout_s=timeout_s,
        poll_s=2,
    )


def delete_pool_nodes(client: Client, nodepool_name: str) -> None:
    """Forcefully delete all nodes in *nodepool_name*. Karpenter will re-provision as needed."""
    nodes = get_pool_nodes(client, nodepool_name)
    for node in nodes:
        if node.metadata and node.metadata.name:
            log.warning("Deleting stale node %s", node.metadata.name)
            try:
                client.delete(Node, node.metadata.name)
            except Exception:
                log.warning("  Failed to delete node %s", node.metadata.name)


def drain_pool_workloads(client: Client, nodepool_name: str, test_namespace: str) -> None:
    """Delete non-daemonset, non-system pods from pool nodes. Best-effort."""
    nodes = get_pool_nodes(client, nodepool_name)
    node_names = {n.metadata.name for n in nodes if n.metadata}
    if not node_names:
        return

    # List all pods, delete workloads on target nodes
    skip_namespaces = {
        "kube-system",
        "karpenter",
        "harbor-system",
        "arc-systems",
        "arc-runners",
        "buildkit",
    }
    for pod in client.list(PodResource, namespace=""):
        if not pod.spec or not pod.metadata:
            continue
        if pod.spec.nodeName not in node_names:
            continue
        if pod.metadata.namespace in skip_namespaces:
            continue
        if pod.metadata.namespace == test_namespace:
            continue
        # Skip daemonset pods
        if pod.metadata.ownerReferences and any(ref.kind == "DaemonSet" for ref in pod.metadata.ownerReferences):
            continue
        log.warning(
            "Draining pod %s/%s from %s",
            pod.metadata.namespace,
            pod.metadata.name,
            pod.spec.nodeName,
        )
        try:
            client.delete(
                PodResource,
                pod.metadata.name,
                namespace=pod.metadata.namespace,
            )
        except Exception:
            log.warning(
                "  Failed to delete %s/%s",
                pod.metadata.namespace,
                pod.metadata.name,
            )


# ---------------------------------------------------------------------------
# Config switching and log search
# ---------------------------------------------------------------------------


def reconfigure_compactor(
    client: Client,
    env_overrides: dict[str, str],
    compactor_logs: object,
) -> dict[str, str]:
    """Switch compactor config: patch env, wait for rollout, wait for first reconcile.

    Returns the original env values (for optional restore).
    The *compactor_logs* parameter is the CompactorLogCollector — its ``stop``
    and ``start`` methods are called to reconnect to the new pod.
    """
    old_pod_names = get_compactor_pod_names(client)
    originals = patch_compactor_env(client, env_overrides)

    # Check for no-op (idempotent patch)
    patch_is_noop = all(str(originals.get(k, "")) == str(v) for k, v in env_overrides.items())
    if not patch_is_noop:
        wait_for_compactor_rollout(client, old_pod_names)
        # Reconnect log stream to new pod
        compactor_logs.stop()
        compactor_logs.start()

    # Wait for at least one reconciliation cycle
    time.sleep(15)
    return originals


def get_reserved_nodes(client: Client, nodepool_name: str) -> set[str]:
    """Return names of nodes with the capacity-reserved annotation."""
    result: set[str] = set()
    for node in get_pool_nodes(client, nodepool_name):
        annotations = (node.metadata and node.metadata.annotations) or {}
        if annotations.get("node-compactor.osdc.io/capacity-reserved") == "true":
            result.add(node.metadata.name)
    return result


def get_do_not_disrupt_nodes(client: Client, nodepool_name: str) -> set[str]:
    """Return names of nodes with karpenter.sh/do-not-disrupt=true."""
    result: set[str] = set()
    for node in get_pool_nodes(client, nodepool_name):
        annotations = (node.metadata and node.metadata.annotations) or {}
        if annotations.get("karpenter.sh/do-not-disrupt") == "true":
            result.add(node.metadata.name)
    return result


def cleanup_stale_cluster_state(client: Client, nodepool_name: str) -> None:
    """Remove stale compactor taints and reservation annotations from pool nodes.

    A crashed previous test run may leave behind:
    - ``node-compactor.osdc.io/consolidating`` NoSchedule taints
    - ``node-compactor.osdc.io/capacity-reserved`` annotations
    - ``karpenter.sh/do-not-disrupt`` annotations (set by the compactor)

    This cleans them in-place without deleting nodes or NodeClaims.
    """
    nodes = get_pool_nodes(client, nodepool_name)
    for node in nodes:
        if not node.metadata or not node.metadata.name:
            continue
        name = node.metadata.name

        # Remove compactor taint if present
        if _node_has_taint(node, COMPACTOR_TAINT_KEY):
            log.info("Cleanup: removing stale compactor taint from %s", name)
            try:
                fresh = client.get(Node, name)
                taints = fresh.spec.taints or [] if fresh.spec else []
                new_taints = [
                    {"key": t.key, "effect": t.effect, **({"value": t.value} if t.value else {})}
                    for t in taints
                    if t.key != COMPACTOR_TAINT_KEY
                ]
                patch = {
                    "metadata": {"resourceVersion": fresh.metadata.resourceVersion},
                    "spec": {"taints": new_taints or None},
                }
                client.patch(Node, name, patch, patch_type=PatchType.MERGE)
            except Exception:
                log.warning("Cleanup: failed to remove taint from %s", name)

        # Remove stale reservation annotations
        annotations = node.metadata.annotations or {}
        stale_keys = []
        if annotations.get("node-compactor.osdc.io/capacity-reserved") == "true":
            stale_keys.append("node-compactor.osdc.io/capacity-reserved")
        if annotations.get("karpenter.sh/do-not-disrupt") == "true":
            stale_keys.append("karpenter.sh/do-not-disrupt")

        if stale_keys:
            log.info("Cleanup: removing stale annotations from %s: %s", name, stale_keys)
            patch = {"metadata": {"annotations": dict.fromkeys(stale_keys)}}
            try:
                client.patch(Node, name, patch, patch_type=PatchType.MERGE)
            except Exception:
                log.warning("Cleanup: failed to remove annotations from %s", name)


def search_compactor_logs(collector: object, pattern: str, since_line: int = 0) -> list[str]:
    """Search captured compactor log lines for a regex pattern.

    Args:
        collector: CompactorLogCollector with a `lines` property.
        pattern: Regex pattern to search for.
        since_line: Start searching from this line index (0-based).

    Returns:
        List of matching log lines.
    """
    compiled = re.compile(pattern)
    lines = collector.lines
    return [line for line in lines[since_line:] if compiled.search(line)]
