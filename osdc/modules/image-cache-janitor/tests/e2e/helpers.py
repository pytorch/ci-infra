"""Helpers for image-cache-janitor e2e tests."""

from __future__ import annotations

import json
import logging
import re
import subprocess
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field

from lightkube import Client
from lightkube.models.core_v1 import (
    Container,
    PodSpec,
    ResourceRequirements,
    Toleration,
)
from lightkube.models.meta_v1 import ObjectMeta
from lightkube.resources.apps_v1 import DaemonSet
from lightkube.resources.core_v1 import Pod as PodResource

log = logging.getLogger("e2e")

# ── Constants ────────────────────────────────────────────────────────────────

JANITOR_DAEMONSET = "image-cache-janitor"
JANITOR_NAMESPACE = "kube-system"
KARPENTER_NODEPOOL_LABEL = "karpenter.sh/nodepool"
TEST_IMAGE = "registry.k8s.io/pause:3.10"

_SENTINEL_MISSING = "__E2E_MISSING__"

# Test images to pull for eviction tests. Small images keep pulls fast.
TEST_PULL_IMAGES = [
    "docker.io/library/alpine:3.18",
    "docker.io/library/alpine:3.19",
    "docker.io/library/alpine:3.20",
    "docker.io/library/busybox:1.36",
    "docker.io/library/busybox:1.37",
]


# ── Dataclass ────────────────────────────────────────────────────────────────


@dataclass
class CachedImage:
    """A container image in the node's image cache."""

    id: str
    tags: list[str] = field(default_factory=list)
    size: int = 0
    pinned: bool = False


# ── Polling ──────────────────────────────────────────────────────────────────


def wait_for(
    description: str,
    check_fn: Callable[[], bool],
    timeout_s: int = 300,
    poll_s: int = 10,
    on_timeout: Callable[[], str] | None = None,
) -> None:
    """Poll *check_fn* until it returns ``True`` or *timeout_s* elapses."""
    log.info("Waiting for: %s", description)
    deadline = time.monotonic() + timeout_s
    while True:
        if check_fn():
            log.info("OK: %s", description)
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            msg = f"Timed out after {timeout_s}s waiting for: {description}"
            if on_timeout:
                msg += f"\n{on_timeout()}"
            raise TimeoutError(msg)
        time.sleep(min(poll_s, remaining))


# ── crictl wrappers ──────────────────────────────────────────────────────────


def exec_crictl(pod_name: str, *args: str, timeout: int = 120) -> str:
    """Run a crictl command via kubectl exec into the janitor pod."""
    cmd = [
        "kubectl",
        "exec",
        "-n",
        JANITOR_NAMESPACE,
        pod_name,
        "--",
        "crictl",
        *args,
    ]
    log.debug("exec_crictl: %s", " ".join(args))
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        log.warning(
            "crictl %s failed (rc=%d): %s",
            args[0] if args else "?",
            result.returncode,
            result.stderr.strip(),
        )
    result.check_returncode()
    return result.stdout


def get_cached_images(pod_name: str) -> list[CachedImage]:
    """List all cached container images on the node."""
    raw = exec_crictl(pod_name, "images", "-o", "json")
    data = json.loads(raw)
    images = []
    for entry in data.get("images", []):
        size = entry.get("size", 0)
        if isinstance(size, str):
            size = int(size)
        images.append(
            CachedImage(
                id=entry["id"],
                tags=entry.get("repoTags") or [],
                size=size,
                pinned=entry.get("pinned", False),
            )
        )
    return images


def get_cache_size_bytes(pod_name: str) -> int:
    """Total bytes of all cached images on the node."""
    return sum(img.size for img in get_cached_images(pod_name))


def pull_image(pod_name: str, image_ref: str, timeout: int = 300) -> None:
    """Pull a container image onto the node via crictl."""
    log.info("Pulling image: %s", image_ref)
    exec_crictl(pod_name, "pull", image_ref, timeout=timeout)


def image_ids_on_node(pod_name: str) -> set[str]:
    """Return the set of image IDs currently cached."""
    return {img.id for img in get_cached_images(pod_name)}


# ── Logs ─────────────────────────────────────────────────────────────────────


def get_janitor_logs(pod_name: str) -> list[str]:
    """Get all log lines from a janitor pod."""
    cmd = ["kubectl", "logs", "-n", JANITOR_NAMESPACE, pod_name]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        return []
    return result.stdout.splitlines()


def search_logs(lines: list[str], pattern: str) -> list[str]:
    """Return log lines matching a regex *pattern*."""
    compiled = re.compile(pattern)
    return [line for line in lines if compiled.search(line)]


def parse_eviction_sizes(lines: list[str]) -> list[float]:
    """Parse eviction log entries and return sizes in MiB (first cycle only).

    Scans for ``Removing: TAG (X.Y MiB)`` lines between the first
    ``Evicting N images`` summary and the next ``Image cache:`` marker.
    """
    sizes: list[float] = []
    in_cycle = False
    for line in lines:
        if "Evicting" in line and "to reach target" in line:
            in_cycle = True
            sizes = []
        elif "Image cache:" in line and in_cycle:
            break  # next cycle started
        elif "Removing:" in line and in_cycle:
            match = re.search(r"\((\d+\.?\d*) MiB\)", line)
            if match:
                sizes.append(float(match.group(1)))
    return sizes


# ── Metrics ──────────────────────────────────────────────────────────────────


def fetch_metrics(pod_name: str) -> str:
    """Scrape Prometheus metrics from the janitor's ``/metrics`` endpoint."""
    cmd = [
        "kubectl",
        "exec",
        "-n",
        JANITOR_NAMESPACE,
        pod_name,
        "--",
        "python3",
        "-c",
        "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:9103/metrics').read().decode())",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    result.check_returncode()
    return result.stdout


def parse_metric_value(metrics_text: str, metric_name: str) -> float:
    """Extract a single metric value from Prometheus text exposition format."""
    for line in metrics_text.splitlines():
        if line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[0] == metric_name:
            return float(parts[1])
    raise ValueError(f"Metric {metric_name!r} not found")


# ── DaemonSet management ────────────────────────────────────────────────────


def patch_janitor_env(
    client: Client,
    env_overrides: dict[str, str],
) -> dict[str, str]:
    """Patch the janitor DaemonSet env vars. Returns originals for restore."""
    ds = client.get(DaemonSet, JANITOR_DAEMONSET, namespace=JANITOR_NAMESPACE)
    container = ds.spec.template.spec.containers[0]
    existing = {e.name: e.value for e in (container.env or [])}

    originals = {}
    for key in env_overrides:
        originals[key] = existing.get(key, _SENTINEL_MISSING)

    merged = dict(existing)
    merged.update(env_overrides)
    env_list = [{"name": k, "value": str(v)} for k, v in merged.items()]
    patch = {
        "spec": {
            "template": {
                "spec": {
                    "containers": [{"name": container.name, "env": env_list}],
                }
            }
        },
    }
    client.patch(DaemonSet, JANITOR_DAEMONSET, patch, namespace=JANITOR_NAMESPACE)
    log.info("Patched janitor env: %s", env_overrides)
    return originals


def restore_janitor_env(client: Client, originals: dict[str, str]) -> None:
    """Restore janitor DaemonSet env vars from saved originals."""
    restore = {k: v for k, v in originals.items() if v != _SENTINEL_MISSING}
    drop_keys = {k for k, v in originals.items() if v == _SENTINEL_MISSING}

    ds = client.get(DaemonSet, JANITOR_DAEMONSET, namespace=JANITOR_NAMESPACE)
    container = ds.spec.template.spec.containers[0]
    existing = {e.name: e.value for e in (container.env or [])}

    merged = dict(existing)
    merged.update(restore)
    for key in drop_keys:
        merged.pop(key, None)

    env_list = [{"name": k, "value": str(v)} for k, v in merged.items()]
    patch = {
        "spec": {
            "template": {
                "spec": {
                    "containers": [{"name": container.name, "env": env_list}],
                }
            }
        },
    }
    client.patch(DaemonSet, JANITOR_DAEMONSET, patch, namespace=JANITOR_NAMESPACE)
    log.info("Restored janitor env to originals")


# ── Pod helpers ──────────────────────────────────────────────────────────────


def get_janitor_pod_on_node(client: Client, node_name: str) -> str | None:
    """Find the running, non-terminating janitor pod on *node_name*."""
    for pod in client.list(
        PodResource,
        namespace=JANITOR_NAMESPACE,
        labels={"app": "image-cache-janitor"},
    ):
        if (
            pod.spec.nodeName == node_name
            and pod.status
            and pod.status.phase == "Running"
            and not pod.metadata.deletionTimestamp
        ):
            return pod.metadata.name
    return None


def wait_for_janitor_pod(
    client: Client,
    node_name: str,
    timeout_s: int = 300,
) -> str:
    """Wait for a running janitor pod on *node_name*. Returns pod name."""
    pod_name: str | None = None

    def _check() -> bool:
        nonlocal pod_name
        pod_name = get_janitor_pod_on_node(client, node_name)
        return pod_name is not None

    wait_for(
        f"janitor pod running on {node_name}",
        _check,
        timeout_s=timeout_s,
        poll_s=5,
    )
    assert pod_name is not None
    return pod_name


def wait_for_janitor_rollout(
    client: Client,
    node_name: str,
    old_pod_name: str,
    timeout_s: int = 120,
) -> str:
    """Wait for a NEW janitor pod on the node (different from *old_pod_name*)."""
    new_pod: str | None = None

    def _check() -> bool:
        nonlocal new_pod
        pod = get_janitor_pod_on_node(client, node_name)
        if pod is not None and pod != old_pod_name:
            new_pod = pod
            return True
        return False

    wait_for(
        f"new janitor pod on {node_name} (replacing {old_pod_name})",
        _check,
        timeout_s=timeout_s,
        poll_s=5,
    )
    assert new_pod is not None
    return new_pod


def create_test_pod(
    client: Client,
    name: str,
    namespace: str,
    nodepool: str,
    instance_type: str,
) -> None:
    """Create a minimal pause pod targeting a NodePool."""
    pod = PodResource(
        metadata=ObjectMeta(name=name, namespace=namespace),
        spec=PodSpec(
            restartPolicy="Never",
            terminationGracePeriodSeconds=0,
            nodeSelector={KARPENTER_NODEPOOL_LABEL: nodepool},
            # Tolerate all taints — the nodeSelector constrains placement;
            # tolerations just need to let the pod past whatever taints
            # exist on runner/buildkit nodes (instance-type, workload/*,
            # git-cache-not-ready, nvidia.com/gpu, etc.).
            tolerations=[Toleration(operator="Exists")],
            containers=[
                Container(
                    name="pause",
                    image=TEST_IMAGE,
                    resources=ResourceRequirements(
                        requests={"cpu": "100m", "memory": "64Mi"},
                    ),
                ),
            ],
        ),
    )
    client.create(pod)
    log.info("Created test pod %s/%s", namespace, name)


def delete_all_pods(client: Client, namespace: str) -> None:
    """Delete every pod in *namespace* (best-effort)."""
    for pod in client.list(PodResource, namespace=namespace):
        with suppress(Exception):
            client.delete(PodResource, pod.metadata.name, namespace=namespace)
