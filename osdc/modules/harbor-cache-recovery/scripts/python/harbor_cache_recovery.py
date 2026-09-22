#!/usr/bin/env python3
"""Harbor proxy cache recovery.

Scans all pods for ImagePullBackOff errors caused by Harbor proxy cache
corruption (stale manifests, size mismatches). When detected, purges the
cached artifact from Harbor so the next pull re-fetches from upstream.

Never deletes pods — only purges the Harbor cache entry.
"""

import itertools
import logging
import os
import sys
import time
from collections import Counter

import requests
from harbor_client import PurgeOutcome, create_harbor_session, fetch_csrf_token, purge_cached_artifact
from lightkube import Client
from pull_failures import find_pull_failures, log_detections, select_purge_targets

log = logging.getLogger("harbor-cache-recovery")

DEFAULT_MIN_POD_AGE_SECONDS = 120
DEFAULT_HARBOR_URL = "http://harbor.harbor-system.svc.cluster.local:80"

# Harbor exposes no bulk delete, so targets are purged one request at a time against the
# CronJob's activeDeadlineSeconds of 300. Tag cardinality is unbounded — a registry-wide
# incident makes every distinct tag its own target — so the queue is capped and the
# remainder is left for the next tick rather than risking a DeadlineExceeded kill.
MAX_PURGES_PER_RUN = 200


def get_config() -> dict:
    return {
        "harbor_url": os.environ.get("HARBOR_URL", DEFAULT_HARBOR_URL),
        "harbor_password": os.environ.get("HARBOR_ADMIN_PASSWORD", ""),
        "min_pod_age_seconds": int(os.environ.get("MIN_POD_AGE_SECONDS", str(DEFAULT_MIN_POD_AGE_SECONDS))),
        "dry_run": os.environ.get("DRY_RUN", "false").lower() in ("true", "1", "yes"),
    }


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    config = get_config()
    if not config["harbor_password"]:
        log.error("HARBOR_ADMIN_PASSWORD not set")
        return 1

    log.info(
        "Starting: dry_run=%s min_age=%ds",
        config["dry_run"],
        config["min_pod_age_seconds"],
    )

    kube = Client()
    start = time.monotonic()

    try:
        failures = find_pull_failures(kube, config["min_pod_age_seconds"])
    except Exception:
        log.exception("Failed to scan pods")
        return 1

    if not failures:
        log.info("No cache-related pull failures found")
        return 0

    log_detections(failures)
    targets = select_purge_targets(failures)

    if not targets:
        log.info("Nothing purgeable from %d failing containers", len(failures))
        return 0

    log.info("%d unique artifacts from %d failing containers", len(targets), len(failures))

    if len(targets) > MAX_PURGES_PER_RUN:
        log.warning(
            "Capping at %d artifacts this run; %d deferred to the next run",
            MAX_PURGES_PER_RUN,
            len(targets) - MAX_PURGES_PER_RUN,
        )
        targets = dict(itertools.islice(targets.items(), MAX_PURGES_PER_RUN))

    if config["dry_run"]:
        for key in targets:
            log.info("DRY RUN: would purge %s", key)
        return 0

    session = create_harbor_session(config["harbor_url"], config["harbor_password"])
    try:
        fetch_csrf_token(session, config["harbor_url"])
    except requests.RequestException:
        log.exception("Failed to connect to Harbor")
        return 1

    outcomes = Counter(
        purge_cached_artifact(
            session, config["harbor_url"], info["harbor_project"], info["repo_path"], info["reference"]
        )
        for info in targets.values()
    )

    elapsed = time.monotonic() - start
    log.info(
        "Done in %.1fs: %d purged, %d absent, %d referenced, %d unresolved, %d failed",
        elapsed,
        outcomes[PurgeOutcome.PURGED],
        outcomes[PurgeOutcome.ABSENT],
        outcomes[PurgeOutcome.REFERENCED],
        outcomes[PurgeOutcome.UNRESOLVED],
        outcomes[PurgeOutcome.FAILED],
    )
    return 1 if outcomes[PurgeOutcome.FAILED] > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
