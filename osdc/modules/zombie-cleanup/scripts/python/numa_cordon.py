#!/usr/bin/env python3
"""NUMA trap-node cordoner for ARC GPU runner namespaces.

Detects pods that failed with TopologyAffinityError (kubelet rejected the
pod because the requested GPUs could not fit on a single NUMA socket) and
cordons the offending node so the scheduler stops routing new GPU pods to
it. This breaks the fragmentation livelock described in
https://github.com/pytorch/ci-infra/issues/696.

Cordoned nodes drain naturally as existing pods finish. Karpenter will
provision replacement capacity if needed. A separate uncordon pass
(run with UNCORDON_ENABLED=true) removes the cordon once all GPU pods
on the node have finished, returning it to the schedulable pool.
"""

import logging
import os
import sys
import time
from datetime import UTC, datetime

from lightkube import Client
from lightkube.core.exceptions import ApiError
from lightkube.models.meta_v1 import ObjectMeta
from lightkube.resources.core_v1 import Node, Pod
from lightkube.types import PatchType

log = logging.getLogger("numa-cordon")

# Annotation set on nodes we cordon so we only uncordon our own work.
CORDON_ANNOTATION = "osdc.io/numa-cordoned"


def get_config() -> dict:
    return {
        "namespace": os.environ.get("TARGET_NAMESPACE", "arc-runners"),
        "dry_run": os.environ.get("DRY_RUN", "false").lower() in ("true", "1", "yes"),
        "uncordon_enabled": os.environ.get("UNCORDON_ENABLED", "true").lower()
        in ("true", "1", "yes"),
    }


def find_topology_failed_pods(client: Client, namespace: str) -> list[Pod]:
    """Find pods that failed with TopologyAffinityError."""
    failed = []
    for pod in client.list(Pod, namespace=namespace):
        if pod.status is None:
            continue
        if pod.status.phase != "Failed":
            continue
        reason = getattr(pod.status, "reason", None) or ""
        if reason == "TopologyAffinityError":
            failed.append(pod)
    return failed


def cordon_nodes(
    client: Client, pods: list[Pod], dry_run: bool
) -> tuple[set[str], int, int]:
    """Cordon nodes that rejected pods with TopologyAffinityError.

    Returns (cordoned_node_names, cordon_count, fail_count).
    """
    # Collect unique node names from failed pods.
    node_names: dict[str, list[str]] = {}
    for pod in pods:
        node = getattr(pod.spec, "nodeName", None)
        if not node:
            log.warning(
                "Pod %s has TopologyAffinityError but no nodeName, skipping",
                pod.metadata.name,
            )
            continue
        node_names.setdefault(node, []).append(pod.metadata.name)

    cordoned: set[str] = set()
    failed = 0

    for node_name, pod_names in node_names.items():
        # Check if already unschedulable.
        try:
            node_obj = client.get(Node, name=node_name)
        except ApiError as e:
            if e.status.code == 404:
                log.info("Node %s no longer exists, skipping", node_name)
                continue
            log.exception("Failed to get node %s", node_name)
            failed += 1
            continue

        if getattr(node_obj.spec, "unschedulable", False):
            log.info(
                "Node %s already unschedulable, skipping cordon (pods: %s)",
                node_name,
                ", ".join(pod_names),
            )
            cordoned.add(node_name)
            continue

        if dry_run:
            log.info(
                "DRY RUN: would cordon node %s (pods: %s)",
                node_name,
                ", ".join(pod_names),
            )
            cordoned.add(node_name)
            continue

        # Cordon: set spec.unschedulable = true and annotate.
        patch = {
            "spec": {"unschedulable": True},
            "metadata": {
                "annotations": {
                    CORDON_ANNOTATION: datetime.now(UTC).isoformat(),
                }
            },
        }
        try:
            client.patch(Node, name=node_name, obj=patch, patch_type=PatchType.MERGE)
            log.info(
                "Cordoned node %s (pods: %s)", node_name, ", ".join(pod_names)
            )
            cordoned.add(node_name)
        except Exception:
            log.exception("Failed to cordon node %s", node_name)
            failed += 1

    return cordoned, len(cordoned), failed


def cleanup_failed_pods(
    client: Client,
    pods: list[Pod],
    cordoned_nodes: set[str],
    namespace: str,
    dry_run: bool,
) -> tuple[int, int]:
    """Delete failed TopologyAffinityError pods on cordoned nodes.

    Returns (deleted_count, fail_count).
    """
    deleted = 0
    failed = 0
    for pod in pods:
        node = getattr(pod.spec, "nodeName", None)
        if node not in cordoned_nodes:
            continue
        name = pod.metadata.name
        if dry_run:
            log.info("DRY RUN: would delete failed pod %s", name)
            deleted += 1
            continue
        try:
            client.delete(Pod, name=name, namespace=namespace)
            log.info("Deleted failed pod %s", name)
            deleted += 1
        except ApiError as e:
            if e.status.code == 404:
                log.info("Pod %s already gone", name)
                deleted += 1
            else:
                log.exception("Failed to delete pod %s", name)
                failed += 1
        except Exception:
            log.exception("Failed to delete pod %s", name)
            failed += 1
    return deleted, failed


def uncordon_drained_nodes(client: Client, dry_run: bool) -> tuple[int, int]:
    """Uncordon nodes we previously cordoned that no longer have GPU pods.

    Only touches nodes bearing our CORDON_ANNOTATION.
    Returns (uncordoned_count, fail_count).
    """
    uncordoned = 0
    failed = 0

    for node in client.list(Node):
        annotations = node.metadata.annotations or {}
        if CORDON_ANNOTATION not in annotations:
            continue
        if not getattr(node.spec, "unschedulable", False):
            # Already schedulable — clean up stale annotation.
            _remove_annotation(client, node.metadata.name, dry_run)
            continue

        # Check if any GPU pods remain on this node.
        has_gpu_pods = False
        field_sel = f"spec.nodeName={node.metadata.name}"
        for pod in client.list(Pod, field_selector=field_sel):
            if pod.status and pod.status.phase in ("Succeeded", "Failed"):
                continue
            containers = []
            if pod.spec and pod.spec.containers:
                containers = pod.spec.containers
            for c in containers:
                limits = {}
                if c.resources and c.resources.limits:
                    limits = c.resources.limits
                if "nvidia.com/gpu" in limits:
                    has_gpu_pods = True
                    break
            if has_gpu_pods:
                break

        if has_gpu_pods:
            log.info(
                "Node %s still has active GPU pods, keeping cordoned",
                node.metadata.name,
            )
            continue

        if dry_run:
            log.info("DRY RUN: would uncordon node %s", node.metadata.name)
            uncordoned += 1
            continue

        patch = {
            "spec": {"unschedulable": False},
            "metadata": {"annotations": {CORDON_ANNOTATION: None}},
        }
        try:
            client.patch(
                Node, name=node.metadata.name, obj=patch, patch_type=PatchType.MERGE
            )
            log.info("Uncordoned node %s (GPU pods drained)", node.metadata.name)
            uncordoned += 1
        except Exception:
            log.exception("Failed to uncordon node %s", node.metadata.name)
            failed += 1

    return uncordoned, failed


def _remove_annotation(client: Client, node_name: str, dry_run: bool) -> None:
    """Remove our cordon annotation from a node that's already schedulable."""
    if dry_run:
        return
    patch = {"metadata": {"annotations": {CORDON_ANNOTATION: None}}}
    try:
        client.patch(Node, name=node_name, obj=patch, patch_type=PatchType.MERGE)
    except Exception:
        log.exception("Failed to remove annotation from node %s", node_name)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    config = get_config()
    log.info(
        "Starting numa-cordon: namespace=%s dry_run=%s uncordon_enabled=%s",
        config["namespace"],
        config["dry_run"],
        config["uncordon_enabled"],
    )

    client = Client()
    start = time.monotonic()

    try:
        # Phase 1: Find and cordon.
        pods = find_topology_failed_pods(client, config["namespace"])
        if pods:
            log.info("Found %d pod(s) with TopologyAffinityError", len(pods))
            cordoned, cordon_ok, cordon_fail = cordon_nodes(
                client, pods, config["dry_run"]
            )
            del_ok, del_fail = cleanup_failed_pods(
                client, pods, cordoned, config["namespace"], config["dry_run"]
            )
            log.info(
                "Cordon phase: nodes_cordoned=%d cordon_failed=%d "
                "pods_deleted=%d pod_delete_failed=%d",
                cordon_ok,
                cordon_fail,
                del_ok,
                del_fail,
            )
        else:
            log.info("No TopologyAffinityError pods found")
            cordon_fail = 0
            del_fail = 0

        # Phase 2: Uncordon drained nodes.
        uncordon_fail = 0
        if config["uncordon_enabled"]:
            uncordon_ok, uncordon_fail = uncordon_drained_nodes(
                client, config["dry_run"]
            )
            log.info(
                "Uncordon phase: nodes_uncordoned=%d uncordon_failed=%d",
                uncordon_ok,
                uncordon_fail,
            )

        elapsed = time.monotonic() - start
        log.info("Completed in %.1fs", elapsed)
        return 1 if (cordon_fail + del_fail + uncordon_fail) > 0 else 0

    except Exception as e:
        log.exception("numa-cordon failed: %s", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
