#!/usr/bin/env python3
"""Zombie pod cleanup for ARC runner namespaces.

Identifies and deletes pods that have been Pending too long (stuck scheduling)
or Running too long (stuck execution). Skips pods managed by controllers
(ReplicaSets, DaemonSets, StatefulSets, Jobs) to avoid interfering with
long-lived infrastructure components like listener pods or hooks-warmer
DaemonSets.

GitHub Actions runner pods are owned by an EphemeralRunner custom resource, not
by a kind in MANAGED_OWNER_KINDS, so they remain eligible for cleanup. A runner
actively executing a job (see runner_busy) is protected from age-based reaping
until it exceeds a much larger busy hard-cap, at which point it is presumed hung.
Every delete candidate is re-checked against fresh per-pod state immediately
before deletion, so a runner that picks up a job mid-run is never reaped.
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
from runner_busy import (
    Decision,
    classify_pod,
    gather_busy_state,
    pod_phase,
    recheck_liveness,
)

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
        "running_max_hours": int(os.environ.get("RUNNING_MAX_AGE_HOURS", "24")),
        # Hard cap on a busy runner's lifetime before it is presumed hung. Pod age is
        # a valid job-runtime proxy only because every scale set runs minRunners:0 —
        # the pod is created at job assignment, so its age tracks job runtime — and 48h
        # sits far above any legit job (GHA 6h max + prepare timeout). A warm pool
        # (minRunners>0) would break this proxy.
        "busy_max_hours": int(os.environ.get("BUSY_MAX_AGE_HOURS", "48")),
        "dry_run": os.environ.get("DRY_RUN", "false").lower() in ("true", "1", "yes"),
        "pushgateway_url": os.environ.get("PUSHGATEWAY_URL", ""),
    }


def validate_config(config: dict) -> str | None:
    """Return an error message if the age thresholds are unsafe, else None.

    All thresholds must be positive so cleanup can never run with a zero/negative
    age gate that would sweep every pod. busy_max must be >= running_max so a busy
    runner is never reaped sooner than an idle one.
    """
    pending_max = config["pending_max_hours"]
    running_max = config["running_max_hours"]
    busy_max = config["busy_max_hours"]
    if running_max <= 0:
        return f"running_max_age_hours must be > 0 (got {running_max})"
    if pending_max <= 0:
        return f"pending_max_age_hours must be > 0 (got {pending_max})"
    if busy_max < running_max:
        return f"busy_max_age_hours ({busy_max}) must be >= running_max_age_hours ({running_max})"
    return None


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
    """Find pods that qualify as zombies based on age thresholds and runner busy state."""
    namespace = config["namespace"]
    pending_max = config["pending_max_hours"]
    running_max = config["running_max_hours"]
    busy_max = config["busy_max_hours"]
    now = datetime.now(UTC)

    pods = list(client.list(Pod, namespace=namespace))
    state = gather_busy_state(client, namespace, pods)

    zombies: list[Pod] = []
    total_count = 0
    managed_count = 0
    busy_skipped = 0
    job_protected = 0
    failsafe_protected = 0
    max_age = 0.0

    for pod in pods:
        total_count += 1
        if is_managed_pod(pod):
            managed_count += 1
            continue
        if is_terminating(pod):
            continue

        phase = pod_phase(pod)
        age_hours = get_pod_age_hours(pod, now)
        if age_hours < 0:
            continue

        name = pod.metadata.name
        decision = classify_pod(pod, phase, age_hours, pending_max, running_max, busy_max, state.liveness_for(pod))

        if decision.is_delete:
            if decision is Decision.HARD_CAP_DELETE:
                log.warning(
                    "%s presumed hung: age=%.1fh over busy hard cap %dh; selecting for deletion",
                    name,
                    age_hours,
                    busy_max,
                )
            else:
                log.info("Zombie found: %s phase=%s age=%.1fh", name, phase, age_hours)
            zombies.append(pod)
            max_age = max(max_age, age_hours)
        elif decision is Decision.BUSY_PROTECT:
            busy_skipped += 1
        elif decision is Decision.JOBPOD_PROTECT:
            job_protected += 1
        elif decision is Decision.FAIL_SAFE_PROTECT:
            failsafe_protected += 1

    m.pods_total.set(total_count)
    m.pods_managed_skipped.set(managed_count)
    m.zombies_found.set(len(zombies))
    m.oldest_zombie_age_hours.set(max_age)
    m.runner_pods_busy_skipped.set(busy_skipped)
    m.job_pods_protected.set(job_protected)
    m.failsafe_protected.set(failsafe_protected)
    m.ephemeralrunner_read_errors.set(1 if state.er_read_failed else 0)

    return zombies


def delete_zombies(client: Client, zombies: list[Pod], config: dict) -> tuple[int, int, int]:
    """Delete zombie pods after a fresh, per-pod busy/liveness recheck.

    Each candidate is re-classified against state fetched for THAT pod immediately
    before its delete, so a runner that became busy — or a job pod whose runner is
    still live — between selection and deletion is skipped. Returns
    (deleted, failed, recheck_skipped).
    """
    namespace = config["namespace"]
    pending_max = config["pending_max_hours"]
    running_max = config["running_max_hours"]
    busy_max = config["busy_max_hours"]
    dry_run = config["dry_run"]
    now = datetime.now(UTC)

    deleted = 0
    failed = 0
    recheck_skipped = 0
    er_read_failed = False

    for pod in zombies:
        name = pod.metadata.name
        phase = pod_phase(pod) or "Unknown"
        age_hours = get_pod_age_hours(pod, now)

        liveness = recheck_liveness(client, pod, namespace)
        er_read_failed = er_read_failed or liveness.read_failed
        decision = classify_pod(pod, phase, age_hours, pending_max, running_max, busy_max, liveness)

        if not decision.is_delete:
            log.info("Recheck: skipping %s — %s", name, decision.value)
            recheck_skipped += 1
            continue

        if dry_run:
            log.info("DRY RUN: would delete %s (phase=%s, %s)", name, phase, decision.value)
            deleted += 1
            continue

        succeeded = False
        try:
            client.delete(Pod, name=name, namespace=namespace)
            log.info("Deleted zombie pod: %s (phase=%s)", name, phase)
            succeeded = True
        except ApiError as e:
            if e.status.code == 404:
                log.info("Pod %s already gone (404), counting as success", name)
                succeeded = True
            else:
                log.exception("Failed to delete pod %s (HTTP %s)", name, e.status.code)
                failed += 1
        except Exception:
            log.exception("Failed to delete pod %s", name)
            failed += 1

        if succeeded:
            deleted += 1
            if decision is Decision.HARD_CAP_DELETE:
                m.busy_hardcap_deletions_total.inc()

    if er_read_failed:
        m.ephemeralrunner_read_errors.set(1)

    return deleted, failed, recheck_skipped


def main() -> int:
    """Run zombie cleanup. Returns 0 on success, 1 on failure."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    config = get_config()
    log.info(
        "Starting zombie cleanup: namespace=%s pending_max=%dh running_max=%dh busy_max=%dh dry_run=%s",
        config["namespace"],
        config["pending_max_hours"],
        config["running_max_hours"],
        config["busy_max_hours"],
        config["dry_run"],
    )

    config_error = validate_config(config)
    if config_error:
        log.error("Invalid configuration, refusing to run: %s", config_error)
        return 1

    client = Client()
    start_time = time.monotonic()

    try:
        zombies = find_zombie_pods(client, config)
        if not zombies:
            log.info("No zombie pods found")
            m.pods_deleted.set(0)
            m.pods_failed.set(0)
            m.pods_skipped.set(0)
            m.recheck_skipped.set(0)
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
        deleted, failed, recheck_skipped = delete_zombies(client, zombies, config)
        log.info(
            "Cleanup complete: %d deleted, %d failed, %d deferred, %d recheck-skipped",
            deleted,
            failed,
            skipped_count,
            recheck_skipped,
        )
        m.pods_deleted.set(deleted)
        m.pods_failed.set(failed)
        m.pods_skipped.set(skipped_count)
        m.recheck_skipped.set(recheck_skipped)
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
        m.recheck_skipped.set(0)
        m.duration_seconds.set(time.monotonic() - start_time)
        m.runs_total.labels(status="failure").inc()
        if config["pushgateway_url"]:
            m.push_metrics(config["pushgateway_url"])
        return 1


if __name__ == "__main__":
    sys.exit(main())
