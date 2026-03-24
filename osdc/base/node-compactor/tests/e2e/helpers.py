"""Kubernetes helpers for node-compactor e2e tests."""

from __future__ import annotations

import contextlib
import logging
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

log = logging.getLogger("e2e")

# ---------------------------------------------------------------------------
# Constants — keep in sync with scripts/python/models.py
# ---------------------------------------------------------------------------

COMPACTOR_TAINT_KEY = "node-compactor.osdc.io/consolidating"
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
) -> None:
    """Poll ``check_fn`` until it returns True or *timeout_s* expires."""
    deadline = time.monotonic() + timeout_s
    log.info("Waiting for: %s (timeout %ds)", description, timeout_s)
    while True:
        if check_fn():
            log.info("  OK: %s", description)
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"Timed out after {timeout_s}s waiting for: {description}")
        time.sleep(min(poll_s, remaining))


def wait_for_stable(
    description: str,
    state_fn: Callable[[], object],
    stable_s: float = 20,
    timeout_s: int = 120,
    poll_s: int = 5,
) -> None:
    """Poll ``state_fn`` until its return value is unchanged for *stable_s*.

    Use instead of hard ``time.sleep`` when waiting for a controller to
    stabilise — this returns as soon as the state is genuinely stable
    rather than waiting a fixed duration.
    """
    deadline = time.monotonic() + timeout_s
    log.info("Waiting for stable: %s (stable %gs, timeout %ds)", description, stable_s, timeout_s)
    last_state = state_fn()
    stable_since = time.monotonic()
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            elapsed_stable = time.monotonic() - stable_since
            raise TimeoutError(
                f"Timed out after {timeout_s}s waiting for stable: {description} "
                f"(last change {elapsed_stable:.0f}s ago)"
            )
        time.sleep(min(poll_s, remaining))
        current = state_fn()
        if current != last_state:
            last_state = current
            stable_since = time.monotonic()
        elapsed_stable = time.monotonic() - stable_since
        if elapsed_stable >= stable_s:
            log.info("  Stable: %s (unchanged for %gs)", description, elapsed_stable)
            return


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


# ---------------------------------------------------------------------------
# Pod helpers
# ---------------------------------------------------------------------------


def get_pods_by_node(client: Client, namespace: str) -> dict[str, list[str]]:
    """Return ``{node_name: [pod_name, ...]}`` for pods in *namespace*."""
    result: dict[str, list[str]] = {}
    for pod in client.list(PodResource, namespace=namespace):
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
    """Return True when exactly *count* pods in *namespace* are Running."""
    pods = list(client.list(PodResource, namespace=namespace))
    running = [p for p in pods if p.status and p.status.phase == "Running"]
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


def _compactor_pod_running(client: Client) -> bool:
    """Check if a compactor pod is Running."""
    for pod in client.list(
        PodResource,
        namespace=COMPACTOR_NAMESPACE,
        labels={"app.kubernetes.io/name": "node-compactor"},
    ):
        if pod.status and pod.status.phase == "Running":
            return True
    return False


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
