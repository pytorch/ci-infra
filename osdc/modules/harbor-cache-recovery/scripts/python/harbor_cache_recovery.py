#!/usr/bin/env python3
"""Harbor proxy cache recovery.

Scans all pods for ImagePullBackOff errors caused by Harbor proxy cache
corruption (stale manifests, size mismatches). When detected, purges the
cached repository from Harbor so the next pull re-fetches from upstream.

Never deletes pods — only purges the Harbor cache entry.
"""

import logging
import os
import sys
import time
from datetime import UTC, datetime

import requests
from lightkube import Client
from lightkube.resources.core_v1 import Pod
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger("harbor-cache-recovery")

REGISTRY_TO_PROJECT = {
    "docker.io": "dockerhub-cache",
    "ghcr.io": "ghcr-cache",
    "public.ecr.aws": "ecr-public-cache",
    "nvcr.io": "nvcr-cache",
    "registry.k8s.io": "k8s-cache",
    "quay.io": "quay-cache",
}

CACHE_CORRUPTION_INDICATORS = (
    "failed size validation",
    "failed precondition",
    "unexpected content digest",
    "failed to copy",
)

DEFAULT_MIN_POD_AGE_SECONDS = 120
DEFAULT_HARBOR_URL = "http://harbor.harbor-system.svc.cluster.local:80"
HARBOR_REQUEST_TIMEOUT_SECONDS = 10
# Wall-clock budget for the purge loop. Must stay comfortably below
# activeDeadlineSeconds in cronjob.yaml so the script can log a final tally
# before K8s SIGTERMs the pod.
PURGE_BUDGET_SECONDS = 240


def get_config() -> dict:
    return {
        "harbor_url": os.environ.get("HARBOR_URL", DEFAULT_HARBOR_URL),
        "harbor_password": os.environ.get("HARBOR_ADMIN_PASSWORD", ""),
        "min_pod_age_seconds": int(os.environ.get("MIN_POD_AGE_SECONDS", str(DEFAULT_MIN_POD_AGE_SECONDS))),
        "dry_run": os.environ.get("DRY_RUN", "false").lower() in ("true", "1", "yes"),
    }


def parse_image_reference(image: str) -> tuple[str, str] | None:
    """Parse image into (registry, repo_path). Returns None for unknown registries.

    Strips tags and digests — we purge the whole cached repository.

    >>> parse_image_reference("grafana/alloy:v1.14.0")
    ('docker.io', 'grafana/alloy')
    >>> parse_image_reference("ghcr.io/actions/runner:latest")
    ('ghcr.io', 'actions/runner')
    >>> parse_image_reference("nginx")
    ('docker.io', 'library/nginx')
    """
    ref = image.split("@")[0]

    parts = ref.split("/")
    last = parts[-1]
    if ":" in last:
        parts[-1] = last.rsplit(":", 1)[0]
    ref = "/".join(parts)

    parts = ref.split("/", 1)
    if len(parts) == 1:
        registry, repo_path = "docker.io", f"library/{parts[0]}"
    elif "." in parts[0] or ":" in parts[0]:
        registry, repo_path = parts[0], parts[1]
    else:
        registry, repo_path = "docker.io", ref

    if registry not in REGISTRY_TO_PROJECT:
        return None
    return registry, repo_path


def _extract_waiting_failures(statuses: list | None) -> list[dict]:
    """Extract ImagePullBackOff entries with cache corruption indicators."""
    if not statuses:
        return []
    results = []
    for cs in statuses:
        waiting = getattr(cs, "state", None)
        waiting = getattr(waiting, "waiting", None) if waiting else None
        if not waiting:
            continue
        reason = getattr(waiting, "reason", None) or ""
        if reason not in ("ImagePullBackOff", "ErrImagePull"):
            continue
        message = getattr(waiting, "message", None) or ""
        if not any(ind in message for ind in CACHE_CORRUPTION_INDICATORS):
            continue
        results.append({"image": getattr(cs, "image", "") or "", "message": message})
    return results


def find_pull_failures(client: Client, min_pod_age_seconds: int) -> list[dict]:
    """Find containers with cache-corruption ImagePullBackOff errors.

    Returns list of dicts: pod_name, namespace, image, harbor_project, repo_path, message.
    """
    now = datetime.now(UTC)
    failures = []

    for pod in client.list(Pod, namespace="*"):
        created = getattr(pod.metadata, "creationTimestamp", None)
        if created is None:
            continue
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        if (now - created).total_seconds() < min_pod_age_seconds:
            continue

        status = pod.status
        if not status:
            continue

        all_entries = []
        all_entries.extend(_extract_waiting_failures(getattr(status, "containerStatuses", None)))
        all_entries.extend(_extract_waiting_failures(getattr(status, "initContainerStatuses", None)))

        for entry in all_entries:
            parsed = parse_image_reference(entry["image"])
            if parsed is None:
                continue
            registry, repo_path = parsed
            failures.append(
                {
                    "pod_name": pod.metadata.name,
                    "namespace": pod.metadata.namespace,
                    "image": entry["image"],
                    "harbor_project": REGISTRY_TO_PROJECT[registry],
                    "repo_path": repo_path,
                    "message": entry["message"][:200],
                }
            )

    return failures


class _NoCookieJar(requests.cookies.RequestsCookieJar):
    """Cookie jar that refuses to store cookies.

    Harbor sets a ``sid`` session cookie on every response. If the session
    stores it, subsequent mutation requests carry the cookie, which makes
    Harbor enforce CSRF — even though we authenticate with Basic Auth.
    Disabling cookie storage at the jar level avoids this globally.
    """

    def set_cookie(self, *_args, **_kwargs):
        return

    def extract_cookies(self, *_args, **_kwargs):
        return


def create_harbor_session(harbor_url: str, admin_password: str) -> requests.Session:
    session = requests.Session()
    session.cookies = _NoCookieJar()
    session.auth = ("admin", admin_password)
    session.headers.update({"Content-Type": "application/json", "Accept": "application/json"})
    # total=1: a single retry only. Corrupted entries that don't purge this run
    # get another shot in 5 minutes — better than burning the deadline on one
    # slow repo and leaving the rest of the queue untouched.
    retry = Retry(total=1, backoff_factor=1, status_forcelist=[502, 503, 504])
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def fetch_csrf_token(session: requests.Session, harbor_url: str) -> None:
    resp = session.get(f"{harbor_url}/api/v2.0/systeminfo", timeout=HARBOR_REQUEST_TIMEOUT_SECONDS)
    resp.raise_for_status()
    csrf_token = resp.headers.get("X-Harbor-CSRF-Token")
    if csrf_token:
        session.headers["X-Harbor-CSRF-Token"] = csrf_token


def purge_cached_repo(session: requests.Session, harbor_url: str, project: str, repo_path: str) -> bool:
    """Delete a cached repository from a Harbor proxy cache project."""
    # Harbor API requires double-encoded slashes: / → %2F → %252F
    encoded_path = repo_path.replace("/", "%252F")
    url = f"{harbor_url}/api/v2.0/projects/{project}/repositories/{encoded_path}"
    try:
        resp = session.delete(url, timeout=HARBOR_REQUEST_TIMEOUT_SECONDS)
        if resp.status_code == 200:
            log.info("Purged: %s/%s", project, repo_path)
            return True
        if resp.status_code == 404:
            log.info("Already gone: %s/%s", project, repo_path)
            return True
        log.warning("Purge failed %s/%s: HTTP %d %s", project, repo_path, resp.status_code, resp.text[:200])
        return False
    except requests.RequestException:
        log.exception("Purge failed %s/%s", project, repo_path)
        return False


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

    unique_repos: dict[str, dict] = {}
    for f in failures:
        key = f"{f['harbor_project']}/{f['repo_path']}"
        if key not in unique_repos:
            unique_repos[key] = f
        log.info("Detected: %s/%s image=%s", f["namespace"], f["pod_name"], f["image"])

    log.info("%d unique repos from %d failing containers", len(unique_repos), len(failures))

    if config["dry_run"]:
        for key in unique_repos:
            log.info("DRY RUN: would purge %s", key)
        return 0

    session = create_harbor_session(config["harbor_url"], config["harbor_password"])
    try:
        fetch_csrf_token(session, config["harbor_url"])
    except requests.RequestException:
        log.exception("Failed to connect to Harbor")
        return 1

    purged = 0
    failed = 0
    skipped = 0
    for info in unique_repos.values():
        # Bail out before issuing a request that could blow the K8s
        # activeDeadlineSeconds. Remaining repos will be picked up on the next
        # */5 cron run.
        if time.monotonic() - start >= PURGE_BUDGET_SECONDS:
            skipped = len(unique_repos) - purged - failed
            log.warning("Budget %ds exhausted; skipping %d remaining repos", PURGE_BUDGET_SECONDS, skipped)
            break
        if purge_cached_repo(session, config["harbor_url"], info["harbor_project"], info["repo_path"]):
            purged += 1
        else:
            failed += 1

    elapsed = time.monotonic() - start
    log.info("Done in %.1fs: %d purged, %d failed, %d skipped", elapsed, purged, failed, skipped)
    return 1 if failed > 0 or skipped > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
