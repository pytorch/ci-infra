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


def push_metrics(pushgateway_url: str) -> None:
    """Push metrics to Prometheus Pushgateway. Best-effort -- logs warning on failure."""
    try:
        push_to_gateway(pushgateway_url, job="zombie-cleanup", registry=registry)
        log.info("Metrics pushed to %s", pushgateway_url)
    except Exception as e:
        log.warning("Failed to push metrics to %s: %s", pushgateway_url, e)
