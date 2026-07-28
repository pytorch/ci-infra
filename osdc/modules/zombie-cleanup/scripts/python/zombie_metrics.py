"""Prometheus metrics for zombie cleanup."""

import logging

from prometheus_client import CollectorRegistry, Counter, Gauge, push_to_gateway

log = logging.getLogger("zombie-cleanup")

registry = CollectorRegistry()

runs_total = Counter(
    "zombie_cleanup_runs_total",
    "Total zombie cleanup runs",
    ["status"],
    registry=registry,
)
pods_total = Gauge("zombie_cleanup_pods_total", "Total pods listed in namespace", registry=registry)
zombies_found = Gauge("zombie_cleanup_zombies_found", "Zombie pods identified", registry=registry)
pods_deleted = Gauge("zombie_cleanup_pods_deleted", "Pods successfully deleted", registry=registry)
pods_failed = Gauge("zombie_cleanup_pods_failed", "Pods that failed to delete", registry=registry)
pods_skipped = Gauge("zombie_cleanup_pods_skipped", "Pods not attempted", registry=registry)
duration_seconds = Gauge("zombie_cleanup_duration_seconds", "Run duration in seconds", registry=registry)
pods_managed_skipped = Gauge(
    "zombie_cleanup_pods_managed_skipped",
    "Controller-managed pods skipped",
    registry=registry,
)
oldest_zombie_age_hours = Gauge(
    "zombie_cleanup_oldest_zombie_age_hours",
    "Age of oldest zombie in hours",
    registry=registry,
)
runner_pods_busy_skipped = Gauge(
    "zombie_cleanup_runner_pods_busy_skipped",
    "Runner pods skipped because they are actively running a job",
    registry=registry,
)
job_pods_protected = Gauge(
    "zombie_cleanup_job_pods_protected",
    "Workflow/step job pods skipped because their owning runner is still live",
    registry=registry,
)
ephemeralrunner_read_errors = Gauge(
    "zombie_cleanup_ephemeralrunner_read_errors",
    "1 if listing EphemeralRunners failed this run (fail-safe engaged), else 0",
    registry=registry,
)
recheck_skipped = Gauge(
    "zombie_cleanup_recheck_skipped",
    "Delete candidates skipped at the pre-delete recheck (became busy or protected)",
    registry=registry,
)
busy_hardcap_deletions_total = Counter(
    "zombie_cleanup_busy_hardcap_deletions_total",
    "Live runner or workflow/step pods deleted after exceeding the busy hard-cap age (presumed hung)",
    registry=registry,
)


def push_metrics(pushgateway_url: str) -> None:
    """Push metrics to Prometheus Pushgateway. Best-effort -- logs warning on failure."""
    try:
        push_to_gateway(pushgateway_url, job="zombie-cleanup", registry=registry)
        log.info("Metrics pushed to %s", pushgateway_url)
    except Exception as e:
        log.warning("Failed to push metrics to %s: %s", pushgateway_url, e)
