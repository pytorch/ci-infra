#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["requests>=2.31"]
# ///
"""Configure Harbor proxy cache projects for pull-through caching.

Creates registry endpoints and proxy cache projects in Harbor for each
upstream registry. Idempotent: 409 Conflict responses are treated as
success (resource already exists).

When credentials are provided for an endpoint that already exists without
them, the endpoint and its proxy cache project are deleted and recreated.
This is required because Harbor's PUT API does not persist credential
updates — credentials can only be set at creation time.

Usage:
    python configure_harbor_projects.py --admin-password PW [--dockerhub-username U --dockerhub-token T] [--github-username U --github-token T]
"""

import argparse
import sys
import time

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ANSI colors
RED = '\033[0;31m'
GREEN = '\033[0;32m'
YELLOW = '\033[1;33m'
NC = '\033[0m'

# Registry endpoint configurations
# type values: docker-hub (Docker Hub), docker-registry (generic V2), harbor (Harbor)
REGISTRIES = [
    {
        "name": "dockerhub",
        "url": "https://hub.docker.com",
        "type": "docker-hub",
        "project_name": "dockerhub-cache",
    },
    {
        "name": "ghcr",
        "url": "https://ghcr.io",
        "type": "docker-registry",
        "project_name": "ghcr-cache",
    },
    {
        "name": "ecr-public",
        "url": "https://public.ecr.aws",
        "type": "docker-registry",
        "project_name": "ecr-public-cache",
    },
    {
        "name": "nvcr",
        "url": "https://nvcr.io",
        "type": "docker-registry",
        "project_name": "nvcr-cache",
    },
    {
        "name": "k8s-registry",
        "url": "https://registry.k8s.io",
        "type": "docker-registry",
        "project_name": "k8s-cache",
    },
    {
        "name": "quay",
        "url": "https://quay.io",
        "type": "docker-registry",
        "project_name": "quay-cache",
    },
]

DEFAULT_HARBOR_URL = "http://localhost:30002"
DEFAULT_ADMIN_PASSWORD = None


def log_info(msg):
    print(f"{GREEN}>{NC} {msg}")


def log_warn(msg):
    print(f"{YELLOW}!{NC} {msg}")


def log_error(msg):
    print(f"{RED}x{NC} {msg}")


def create_session(harbor_url, admin_password):
    """Create a requests session with retry logic and auth."""
    session = requests.Session()
    session.auth = ("admin", admin_password)
    session.headers.update({
        "Content-Type": "application/json",
        "Accept": "application/json",
    })

    retry = Retry(total=3, backoff_factor=1, status_forcelist=[502, 503, 504])
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)

    return session


def fetch_csrf_token(session, harbor_url):
    """Fetch CSRF token from Harbor.

    Harbor 2.x uses gorilla/csrf which employs a masked-token pattern:
    the _gorilla_csrf cookie holds the base token, while the response
    header X-Harbor-CSRF-Token holds the masked token. The masked token
    from the response header is what must be sent back on POST/PUT/DELETE.
    """
    resp = session.get(f"{harbor_url}/api/v2.0/systeminfo", timeout=10)
    resp.raise_for_status()

    csrf_token = resp.headers.get("X-Harbor-CSRF-Token")
    if csrf_token:
        session.headers["X-Harbor-CSRF-Token"] = csrf_token
        log_info("CSRF token acquired")
    else:
        log_warn("No CSRF token in response headers -- Harbor may have CSRF disabled")


def wait_for_harbor(session, harbor_url, timeout=300):
    """Wait for Harbor API to be ready."""
    log_info(f"Waiting for Harbor API at {harbor_url}...")
    start = time.time()
    while time.time() - start < timeout:
        try:
            resp = session.get(f"{harbor_url}/api/v2.0/health", timeout=5)
            if resp.status_code == 200:
                log_info("Harbor API is ready")
                return True
        except requests.ConnectionError:
            pass
        time.sleep(5)

    log_error(f"Harbor API not ready after {timeout}s")
    return False


def get_registry_info(session, harbor_url, registry_name):
    """Get full info for a registry endpoint by name, or None if not found."""
    resp = session.get(
        f"{harbor_url}/api/v2.0/registries",
        params={"name": registry_name},
        timeout=30,
    )

    if resp.status_code == 200:
        for reg in resp.json():
            if reg["name"] == registry_name:
                return reg

    return None


def _endpoint_has_credentials(registry_info):
    """Check if an existing registry endpoint has credentials configured."""
    cred = registry_info.get("credential", {})
    return bool(cred and cred.get("type"))


def delete_registry_endpoint(session, harbor_url, registry_id, registry_name):
    """Delete a registry endpoint by ID."""
    resp = session.delete(
        f"{harbor_url}/api/v2.0/registries/{registry_id}",
        timeout=30,
    )
    if resp.status_code == 200:
        log_info(f"  Deleted registry endpoint: {registry_name} (id={registry_id})")
        return True
    else:
        log_error(
            f"  Failed to delete registry endpoint {registry_name}: "
            f"{resp.status_code} {resp.text}"
        )
        return False


def delete_project(session, harbor_url, project_name):
    """Delete a proxy cache project by name.

    Proxy cache projects may contain cached repositories. Harbor refuses
    to delete non-empty projects, so we delete all repositories first.
    Cached images are ephemeral and will be re-fetched on next pull.
    """
    # Delete all repositories in the project first
    page = 1
    while True:
        resp = session.get(
            f"{harbor_url}/api/v2.0/projects/{project_name}/repositories",
            params={"page": page, "page_size": 100},
            timeout=30,
        )
        if resp.status_code == 404:
            break  # project doesn't exist
        if resp.status_code != 200:
            break
        repos = resp.json()
        if not repos:
            break
        for repo in repos:
            repo_name = repo["name"]  # format: "project_name/image_name"
            # Strip project prefix to get the repo path, then double-encode
            # slashes: %252F -> nginx decodes to %2F -> Harbor interprets as /
            repo_path = repo_name.split("/", 1)[1] if "/" in repo_name else repo_name
            encoded_path = repo_path.replace("/", "%252F")
            del_resp = session.delete(
                f"{harbor_url}/api/v2.0/projects/{project_name}/repositories/{encoded_path}",
                timeout=30,
            )
            if del_resp.status_code == 200:
                log_info(f"  Deleted cached repo: {repo_name}")
            else:
                log_warn(f"  Could not delete repo {repo_name}: {del_resp.status_code}")
        page += 1

    # Now delete the empty project
    resp = session.delete(
        f"{harbor_url}/api/v2.0/projects/{project_name}",
        timeout=30,
    )
    if resp.status_code == 200:
        log_info(f"  Deleted project: {project_name}")
        return True
    elif resp.status_code == 404:
        return True  # already gone
    else:
        log_error(
            f"  Failed to delete project {project_name}: "
            f"{resp.status_code} {resp.text}"
        )
        return False


def ensure_registry_endpoint(session, harbor_url, registry, credentials=None):
    """Ensure a registry endpoint exists with the correct credentials.

    Harbor's PUT API does not persist credential changes, so when credentials
    need to be added or rotated on an existing endpoint, the endpoint (and its
    proxy cache project) must be deleted and recreated.
    """
    payload = {
        "name": registry["name"],
        "url": registry["url"],
        "type": registry["type"],
        "insecure": False,
    }
    if credentials:
        payload["credential"] = credentials

    existing = get_registry_info(session, harbor_url, registry["name"])

    if existing and credentials and not _endpoint_has_credentials(existing):
        # Endpoint exists without credentials but we have credentials to set.
        # Must delete and recreate (Harbor PUT ignores credential field).
        log_info(f"  Recreating {registry['name']} with credentials...")
        if not delete_project(session, harbor_url, registry["project_name"]):
            return False
        if not delete_registry_endpoint(session, harbor_url, existing["id"], registry["name"]):
            return False
        existing = None  # fall through to creation

    if existing is None:
        # Create new endpoint
        resp = session.post(
            f"{harbor_url}/api/v2.0/registries",
            json=payload,
            timeout=30,
        )
        if resp.status_code == 201:
            cred_note = " (with credentials)" if credentials else ""
            log_info(f"  Created registry endpoint: {registry['name']} -> {registry['url']}{cred_note}")
            return True
        elif resp.status_code == 409:
            log_info(f"  Registry endpoint already exists: {registry['name']}")
            return True
        else:
            log_error(
                f"  Failed to create registry endpoint {registry['name']}: "
                f"{resp.status_code} {resp.text}"
            )
            return False
    else:
        log_info(f"  Registry endpoint already exists: {registry['name']}")
        return True


def create_proxy_cache_project(session, harbor_url, registry):
    """Create a proxy cache project linked to a registry endpoint."""
    # Get registry endpoint ID
    info = get_registry_info(session, harbor_url, registry["name"])
    registry_id = info["id"] if info else None
    if registry_id is None:
        log_error(f"  Could not find registry endpoint ID for: {registry['name']}")
        return False

    payload = {
        "project_name": registry["project_name"],
        "registry_id": registry_id,
        "public": True,
        "metadata": {
            "public": "true",
        },
    }

    resp = session.post(
        f"{harbor_url}/api/v2.0/projects",
        json=payload,
        timeout=30,
    )

    if resp.status_code == 201:
        log_info(f"  Created proxy cache project: {registry['project_name']}")
        return True
    elif resp.status_code == 409:
        log_info(f"  Proxy cache project already exists: {registry['project_name']}")
        return True
    else:
        log_error(
            f"  Failed to create project {registry['project_name']}: "
            f"{resp.status_code} {resp.text}"
        )
        return False


def main():
    parser = argparse.ArgumentParser(
        description="Configure Harbor proxy cache projects"
    )
    parser.add_argument(
        "--harbor-url",
        default=DEFAULT_HARBOR_URL,
        help=f"Harbor API URL (default: {DEFAULT_HARBOR_URL})",
    )
    parser.add_argument(
        "--admin-password",
        default=DEFAULT_ADMIN_PASSWORD,
        help="Harbor admin password",
    )
    parser.add_argument(
        "--dockerhub-username",
        default=None,
        help="Docker Hub username for authenticated pulls",
    )
    parser.add_argument(
        "--dockerhub-token",
        default=None,
        help="Docker Hub access token",
    )
    parser.add_argument(
        "--github-username",
        default=None,
        help="GitHub username for ghcr.io authenticated pulls",
    )
    parser.add_argument(
        "--github-token",
        default=None,
        help="GitHub personal access token for ghcr.io",
    )
    parser.add_argument(
        "--no-wait",
        action="store_true",
        help="Don't wait for Harbor to be ready",
    )
    args = parser.parse_args()

    if not args.admin_password:
        parser.error("--admin-password is required (no default; password is auto-generated at deploy time)")

    # Build per-registry credentials map
    registry_credentials = {}
    if args.dockerhub_username and args.dockerhub_token:
        registry_credentials["dockerhub"] = {
            "type": "basic",
            "access_key": args.dockerhub_username,
            "access_secret": args.dockerhub_token,
        }
        log_info("Docker Hub credentials provided")
    if args.github_username and args.github_token:
        registry_credentials["ghcr"] = {
            "type": "basic",
            "access_key": args.github_username,
            "access_secret": args.github_token,
        }
        log_info("GitHub (ghcr.io) credentials provided")

    session = create_session(args.harbor_url, args.admin_password)

    # Wait for Harbor to be ready
    if not args.no_wait:
        if not wait_for_harbor(session, args.harbor_url):
            return 1

    # Harbor 2.x requires CSRF token for POST/PUT/DELETE requests
    fetch_csrf_token(session, args.harbor_url)

    log_info("Ensuring registry endpoints...")
    for registry in REGISTRIES:
        creds = registry_credentials.get(registry["name"])
        if not ensure_registry_endpoint(session, args.harbor_url, registry, creds):
            return 1

    print()
    log_info("Creating proxy cache projects...")
    for registry in REGISTRIES:
        if not create_proxy_cache_project(session, args.harbor_url, registry):
            return 1

    print()
    log_info("Harbor proxy cache configuration complete!")
    log_info("Projects created:")
    for registry in REGISTRIES:
        log_info(f"  {registry['project_name']} -> {registry['url']}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
