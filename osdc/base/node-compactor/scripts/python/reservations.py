"""Capacity reservation management for the Node Compactor.

Annotates selected nodes with karpenter.sh/do-not-disrupt to prevent
Karpenter from deleting them when empty, maintaining ready-to-use capacity.

Uses a two-annotation ownership pattern:
  - node-compactor.osdc.io/capacity-reserved (our marker)
  - karpenter.sh/do-not-disrupt (Karpenter protection)
Only removes do-not-disrupt from nodes bearing our marker.
"""

import logging

from lightkube import ApiError, Client
from lightkube.resources.core_v1 import Node
from lightkube.types import PatchType
from models import (
    ANNOTATION_CAPACITY_RESERVED,
    ANNOTATION_DO_NOT_DISRUPT,
    NodeState,
)

log = logging.getLogger("compactor")


def apply_reservation(client: Client, node_name: str, dry_run: bool) -> bool:
    """Add capacity-reservation annotations to a node.

    Sets both our ownership marker and Karpenter's do-not-disrupt annotation.
    Uses strategic merge patch (additive, won't remove other annotations).
    """
    if dry_run:
        log.info("[DRY RUN] Would reserve node %s", node_name)
        return True

    patch = {
        "metadata": {
            "annotations": {
                ANNOTATION_CAPACITY_RESERVED: "true",
                ANNOTATION_DO_NOT_DISRUPT: "true",
            }
        }
    }
    try:
        client.patch(Node, node_name, patch, patch_type=PatchType.STRATEGIC)
        log.info("Reserved node %s (do-not-disrupt)", node_name)
        return True
    except ApiError as e:
        if e.status.code == 404:
            log.info("Node %s disappeared, skipping reservation", node_name)
        else:
            log.exception("Failed to reserve node %s", node_name)
        return False


def remove_reservation(client: Client, node_name: str, dry_run: bool, max_retries: int = 3) -> bool:
    """Remove capacity-reservation annotations from a node.

    Only removes do-not-disrupt if our ownership marker is present,
    preventing conflicts with other controllers that may have set it.
    Uses optimistic concurrency (resourceVersion) to avoid TOCTOU races.
    """
    if dry_run:
        log.info("[DRY RUN] Would unreserve node %s", node_name)
        return True

    for attempt in range(max_retries):
        try:
            node = client.get(Node, node_name)
        except ApiError as e:
            if e.status.code == 404:
                log.info("Node %s disappeared, skipping unreserve", node_name)
                return True
            raise

        annotations = (node.metadata and node.metadata.annotations) or {}
        if annotations.get(ANNOTATION_CAPACITY_RESERVED) != "true":
            log.debug("Node %s not owned by us, skipping unreserve", node_name)
            return True

        # Build new annotations dict without our two keys
        new_annotations = {
            k: v for k, v in annotations.items() if k not in (ANNOTATION_CAPACITY_RESERVED, ANNOTATION_DO_NOT_DISRUPT)
        }

        patch = {
            "metadata": {
                "resourceVersion": node.metadata.resourceVersion,
                "annotations": new_annotations if new_annotations else None,
            }
        }
        try:
            client.patch(Node, node_name, patch, patch_type=PatchType.MERGE)
            log.info("Unreserved node %s", node_name)
            return True
        except ApiError as e:
            if e.status.code == 404:
                log.info("Node %s disappeared, skipping unreserve", node_name)
                return True
            if e.status.code == 409:
                log.warning(
                    "Conflict unreserving %s (attempt %d/%d)%s",
                    node_name,
                    attempt + 1,
                    max_retries,
                    ", retrying" if attempt < max_retries - 1 else ", giving up",
                )
                continue
            raise

    return False


def reconcile_reservations(
    client: Client,
    node_states: list[NodeState],
    desired_reserved: set[str],
    dry_run: bool,
) -> dict[str, list[str]]:
    """Reconcile capacity-reservation annotations.

    Compares current reservation state (from node_states) with desired set.
    Adds annotations to newly reserved nodes, removes from no-longer-reserved.

    Returns dict with "added" and "removed" lists of node names.
    """
    currently_reserved = {ns.name for ns in node_states if ns.is_reserved}

    to_add = desired_reserved - currently_reserved
    to_remove = currently_reserved - desired_reserved

    added = []
    for name in sorted(to_add):
        if apply_reservation(client, name, dry_run):
            added.append(name)

    removed = []
    for name in sorted(to_remove):
        if remove_reservation(client, name, dry_run):
            removed.append(name)

    if added:
        log.info("Reserved %d node(s): %s", len(added), ", ".join(added))
    if removed:
        log.info("Unreserved %d node(s): %s", len(removed), ", ".join(removed))

    return {"added": added, "removed": removed}


def cleanup_reservations(client: Client) -> None:
    """Remove capacity-reservation annotations from all nodes.

    Called on graceful shutdown to ensure a clean state.
    Only removes do-not-disrupt from nodes bearing our ownership marker.
    """
    log.info("Cleaning up capacity reservations...")
    count = 0
    failed = 0
    for node in client.list(Node):
        annotations = (node.metadata and node.metadata.annotations) or {}
        if annotations.get(ANNOTATION_CAPACITY_RESERVED) == "true":
            try:
                if remove_reservation(client, node.metadata.name, dry_run=False):
                    count += 1
                else:
                    failed += 1
            except Exception:
                log.exception("Failed to unreserve node %s during cleanup", node.metadata.name)
                failed += 1

    if count:
        log.info("Removed reservations from %d node(s)", count)
    if failed:
        log.warning("Failed to remove reservations from %d node(s)", failed)
    if not count and not failed:
        log.info("No reservations found")
