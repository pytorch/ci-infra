#!/usr/bin/env python3
"""Zombie pod cleanup for ARC runner namespaces.

Identifies and deletes pods that have been Pending too long (stuck scheduling)
or Running too long (stuck execution). Skips pods managed by controllers
(ReplicaSets, DaemonSets, StatefulSets, Jobs) to avoid interfering with
long-lived infrastructure components like listener pods or hooks-warmer
DaemonSets.
"""

import logging
import os
import sys
import time
from datetime import UTC, datetime

import zombie_metrics as m
from lightkube import Client
from lightkube.core.exceptions import ApiError
from lightkube.resources.core_v1 import Pod

log = logging.getLogger("zombie-cleanup")

# Owner kinds that indicate a controller-managed pod — never touch these.
# ReplicaSet = listener pods (via Deployments), DaemonSet = hooks-warmer etc,
# StatefulSet = stateful workloads, Job = CronJob-spawned pods (including ours).
MANAGED_OWNER_KINDS = frozenset({"ReplicaSet", "DaemonSet", "StatefulSet", "Job"})


def get_config() -> dict:
    """Read configuration from environment variables."""
    return {
        "namespace": os.environ.get("TARGET_NAMESPACE", "arc-runners"),
        "pending_max_hours": int(os.environ.get("PENDING_MAX_AGE_HOURS", "24")),
        "running_max_hours": int(os.environ.get("RUNNING_MAX_AGE_HOURS", "12")),
        "dry_run": os.environ.get("DRY_RUN", "false").lower() in ("true", "1", "yes"),
        "pushgateway_url": os.environ.get("PUSHGATEWAY_URL", ""),
    }


def is_managed_pod(pod: Pod) -> bool:
    """Check if pod is managed by a controller we should not touch."""
    refs = pod.metadata.ownerReferences
    if not refs:
        return False
    return any(ref.kind in MANAGED_OWNER_KINDS for ref in refs)


def is_terminating(pod: Pod) -> bool:
    """Check if pod already has a deletionTimestamp (being terminated)."""
    return getattr(pod.metadata, "deletionTimestamp", None) is not None


def get_pod_age_hours(pod: Pod, now: datetime) -> float:
    """Get pod age in hours from creationTimestamp.

    Returns -1.0 if timestamp is missing (caller should skip the pod).
    """
    created = pod.metadata.creationTimestamp
    if created is None:
        log.warning("Pod %s has no creationTimestamp, skipping", pod.metadata.name)
        return -1.0
    # lightkube may return naive datetimes — treat as UTC
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    return (now - created).total_seconds() / 3600


def find_zombie_pods(client: Client, config: dict) -> list[Pod]:
    """Find pods that qualify as zombies based on age thresholds."""
    namespace = config["namespace"]
    pending_max = config["pending_max_hours"]
    running_max = config["running_max_hours"]
    now = datetime.now(UTC)
    zombies = []
    total_count = 0
    managed_count = 0
    max_age = 0.0

    for pod in client.list(Pod, namespace=namespace):
        total_count += 1
        if is_managed_pod(pod):
            managed_count += 1
            continue
        if is_terminating(pod):
            continue

        phase = pod.status.phase if pod.status else None
        age_hours = get_pod_age_hours(pod, now)
        if age_hours < 0:
            continue

        name = pod.metadata.name
        threshold = None

        if phase == "Pending" and age_hours > pending_max:
            threshold = pending_max
        elif phase in ("Running", "Unknown") and age_hours > running_max:
            threshold = running_max

        if threshold is not None:
            log.info(
                "Zombie found: %s phase=%s age=%.1fh (threshold=%dh)",
                name,
                phase,
                age_hours,
                threshold,
            )
            zombies.append(pod)
            if age_hours > max_age:
                max_age = age_hours

    m.pods_total.set(total_count)
    m.pods_managed_skipped.set(managed_count)
    m.zombies_found.set(len(zombies))
    m.oldest_zombie_age_hours.set(max_age)

    return zombies


def delete_zombies(client: Client, zombies: list[Pod], config: dict) -> tuple[int, int]:
    """Delete zombie pods. Returns (deleted_count, failed_count)."""
    namespace = config["namespace"]
    dry_run = config["dry_run"]
    deleted = 0
    failed = 0

    for pod in zombies:
        name = pod.metadata.name
        phase = pod.status.phase if pod.status else "Unknown"

        if dry_run:
            log.info("DRY RUN: would delete %s (phase=%s)", name, phase)
            deleted += 1
            continue

        try:
            client.delete(Pod, name=name, namespace=namespace)
            log.info("Deleted zombie pod: %s (phase=%s)", name, phase)
            deleted += 1
        except ApiError as e:
            if e.status.code == 404:
                log.info("Pod %s already gone (404), counting as success", name)
                deleted += 1
            else:
                log.exception("Failed to delete pod %s (HTTP %s)", name, e.status.code)
                failed += 1
        except Exception as e:
            log.exception("Failed to delete pod %s: %s", name, e)
            failed += 1

    return deleted, failed


def main() -> int:
    """Run zombie cleanup. Returns 0 on success, 1 on failure."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    config = get_config()
    log.info(
        "Starting zombie cleanup: namespace=%s pending_max=%dh running_max=%dh dry_run=%s",
        config["namespace"],
        config["pending_max_hours"],
        config["running_max_hours"],
        config["dry_run"],
    )

    client = Client()
    start_time = time.monotonic()

    try:
        zombies = find_zombie_pods(client, config)
        if not zombies:
            log.info("No zombie pods found")
            m.pods_deleted.set(0)
            m.pods_failed.set(0)
            m.pods_skipped.set(0)
            m.duration_seconds.set(time.monotonic() - start_time)
            m.runs_total.labels(status="success").inc()
            if config["pushgateway_url"]:
                m.push_metrics(config["pushgateway_url"])
            return 0

        total_pods_count = int(m.registry.get_sample_value("zombie_cleanup_pods_total") or 0)
        cleanup_cap = max(int(total_pods_count * 0.1), 10)
        skipped_count = max(len(zombies) - cleanup_cap, 0)
        if skipped_count > 0:
            log.warning(
                "Cleanup cap reached: %d zombies found, cleaning %d, deferring %d",
                len(zombies),
                cleanup_cap,
                skipped_count,
            )
            zombies = zombies[:cleanup_cap]

        log.info("Found %d zombie pod(s) to clean", len(zombies))
        deleted, failed = delete_zombies(client, zombies, config)
        log.info("Cleanup complete: %d deleted, %d failed, %d deferred", deleted, failed, skipped_count)
        m.pods_deleted.set(deleted)
        m.pods_failed.set(failed)
        m.pods_skipped.set(skipped_count)
        m.duration_seconds.set(time.monotonic() - start_time)
        m.runs_total.labels(status="success" if failed == 0 else "failure").inc()
        if config["pushgateway_url"]:
            m.push_metrics(config["pushgateway_url"])
        return 1 if failed > 0 else 0
    except Exception as e:
        log.exception("Cleanup failed: %s", e)
        m.pods_deleted.set(0)
        m.pods_failed.set(0)
        m.pods_skipped.set(0)
        m.duration_seconds.set(time.monotonic() - start_time)
        m.runs_total.labels(status="failure").inc()
        if config["pushgateway_url"]:
            m.push_metrics(config["pushgateway_url"])
        return 1


if __name__ == "__main__":
    sys.exit(main())
