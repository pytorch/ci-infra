#!/usr/bin/env python3
"""Pin the actions/runner image per OSDC commit SHA.

Lookup key is the SHA of the most recent commit touching anything under
``osdc/`` (the project directory). Same SHA -> same ``tag@digest``
within the 20-entry rolling window: re-deploying a recent commit
reproduces the exact image that was deployed the first time it ran.
Entries that age out of the window will re-resolve to whatever is
``latest`` at the next deploy of that SHA.

A miss (new SHA) calls GitHub /releases/latest, resolves the digest via
crane, and prepends an entry to the `arc-runner-version-lock` ConfigMap
in `osdc-system`. Writes use optimistic concurrency (resourceVersion);
on 409 the loser re-reads — if the winner pinned this SHA, the loser
returns the winner's image instead of writing.

Operator override: set `arc.runner_image_tag` in `clusters.yaml`; the
caller bypasses this script entirely in that case.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import requests
from lightkube import Client
from lightkube.core.exceptions import ApiError
from lightkube.models.meta_v1 import ObjectMeta
from lightkube.resources.core_v1 import ConfigMap

GITHUB_RELEASES_URL = "https://api.github.com/repos/actions/runner/releases/latest"
IMAGE_REPO = "ghcr.io/actions/actions-runner"

CM_NAME = "arc-runner-version-lock"
CM_NAMESPACE = "osdc-system"
CM_KEY = "history.json"
HISTORY_MAX = 20

CM_LABELS = {
    "app.kubernetes.io/managed-by": "osdc-deploy-log",
    "osdc.io/lock-kind": "arc-runner-version",
}

OSDC_PATH = "."

REQUEST_TIMEOUT_SECONDS = 10
MAX_WRITE_ATTEMPTS = 5


def osdc_root() -> str:
    env_root = os.environ.get("OSDC_UPSTREAM")
    if env_root:
        return env_root
    return str(Path(__file__).resolve().parents[3])


def osdc_sha() -> str:
    root = osdc_root()
    result = subprocess.run(
        ["git", "-C", root, "log", "-1", "--format=%H", "--", OSDC_PATH],
        check=True,
        capture_output=True,
        text=True,
    )
    sha = result.stdout.strip()
    if not sha:
        raise ValueError(
            f"git log returned no commit history under {root}. "
            "Likely a shallow clone — refetch with full history, or pin "
            "arc.runner_image_tag in clusters.yaml to bypass the resolver."
        )
    return sha


def fetch_latest_tag(token: str | None) -> str:
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    resp = requests.get(GITHUB_RELEASES_URL, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)
    resp.raise_for_status()
    data = resp.json()
    tag = data.get("tag_name")
    if not isinstance(tag, str) or not tag:
        raise ValueError("GitHub releases response missing 'tag_name'")
    return tag.lstrip("v")


def read_history(client: Client) -> tuple[list[dict[str, str]], bool, str | None]:
    try:
        cm = client.get(ConfigMap, name=CM_NAME, namespace=CM_NAMESPACE)
    except ApiError as e:
        if getattr(e.status, "code", None) == 404:
            return [], False, None
        raise
    rv = cm.metadata.resourceVersion if cm.metadata else None
    raw = (cm.data or {}).get(CM_KEY)
    if raw is None:
        return [], True, rv
    parsed = json.loads(raw)
    if not isinstance(parsed, list):
        raise ValueError(f"{CM_KEY} must contain a JSON list")
    for entry in parsed:
        if not isinstance(entry, dict) or not {"osdc_sha", "tag", "digest"} <= entry.keys():
            raise ValueError(f"{CM_KEY} entries must be objects with 'osdc_sha', 'tag', 'digest'")
    return parsed, True, rv


def find_cached_entry(history: list[dict[str, str]], sha: str) -> dict[str, str] | None:
    for entry in history:
        if entry.get("osdc_sha") == sha:
            return entry
    return None


def resolve_digest(tag: str) -> str:
    ref = f"{IMAGE_REPO}:{tag}"
    result = subprocess.run(
        ["crane", "digest", ref],
        check=True,
        capture_output=True,
        text=True,
    )
    digest = result.stdout.strip()
    if not digest.startswith("sha256:"):
        raise ValueError(f"crane digest returned unexpected value: {digest!r}")
    return digest


def update_history(
    history: list[dict[str, str]],
    sha: str,
    tag: str,
    digest: str,
    now: datetime,
) -> list[dict[str, str]]:
    new_entry = {
        "osdc_sha": sha,
        "tag": tag,
        "digest": digest,
        "resolved_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    kept = [e for e in history if e.get("osdc_sha") != sha]
    return [new_entry, *kept][:HISTORY_MAX]


def write_history(
    client: Client,
    history: list[dict[str, str]],
    cm_exists: bool,
    resource_version: str | None,
) -> None:
    payload = json.dumps(history, indent=2)
    metadata = ObjectMeta(name=CM_NAME, namespace=CM_NAMESPACE, labels=CM_LABELS)
    if cm_exists and resource_version is not None:
        metadata.resourceVersion = resource_version
    cm = ConfigMap(metadata=metadata, data={CM_KEY: payload})
    if cm_exists:
        client.replace(cm)
    else:
        client.create(cm)


def build_client() -> Client:
    _force_ipv4()
    return Client()


def _force_ipv4() -> None:
    # AWS EKS endpoints advertise AAAA records but reject IPv6 connects from
    # many corporate/home networks. Python's default getaddrinfo returns IPv6
    # first and socket.create_connection then blocks for the full OS timeout
    # (~100s) before falling back. kubectl (Go) handles this with happy-eyeballs;
    # stdlib socket does not. Pin to IPv4 to avoid the dead-end.
    original = socket.getaddrinfo

    def ipv4_only(host, port, family=0, *args, **kwargs):
        if family in (0, socket.AF_UNSPEC):
            family = socket.AF_INET
        return original(host, port, family, *args, **kwargs)

    socket.getaddrinfo = ipv4_only


def now_utc() -> datetime:
    return datetime.now(UTC)


def _run(cluster_id: str, client: Client) -> str:
    print(f"resolve_runner_version: cluster={cluster_id}", file=sys.stderr)

    sha = osdc_sha()
    print(f"resolve_runner_version: osdc_sha={sha}", file=sys.stderr)

    history, cm_exists, rv = read_history(client)

    cached = find_cached_entry(history, sha)
    if cached is not None:
        tag, digest = cached["tag"], cached["digest"]
        print(f"resolve_runner_version: cache hit, {tag}@{digest}", file=sys.stderr)
        return f"{IMAGE_REPO}:{tag}@{digest}"

    if os.environ.get("OSDC_RESOLVER_READONLY"):
        if not history:
            raise ValueError(
                f"OSDC_RESOLVER_READONLY set but {CM_NAME} has no entry for osdc_sha={sha}. "
                "Run a normal deploy (without OSDC_RESOLVER_READONLY) to populate it first."
            )
        newest = history[0]
        newest_sha = newest.get("osdc_sha", "<unknown>")
        newest_resolved_at = newest.get("resolved_at", "<unknown>")
        print(
            f"resolve_runner_version: OSDC_RESOLVER_READONLY set, no cache entry for "
            f"osdc_sha={sha}; falling back to newest entry {newest_sha} resolved at "
            f"{newest_resolved_at}. To pin to your exact SHA, run a normal deploy first.",
            file=sys.stderr,
        )
        tag, digest = newest["tag"], newest["digest"]
        return f"{IMAGE_REPO}:{tag}@{digest}"

    token = os.environ.get("GITHUB_TOKEN") or None
    tag = fetch_latest_tag(token)
    print(f"resolve_runner_version: latest release tag={tag}", file=sys.stderr)

    print("resolve_runner_version: resolving digest via crane", file=sys.stderr)
    digest = resolve_digest(tag)
    print(f"resolve_runner_version: resolved digest={digest}", file=sys.stderr)

    for attempt in range(MAX_WRITE_ATTEMPTS):
        if attempt > 0:
            history, cm_exists, rv = read_history(client)
            cached = find_cached_entry(history, sha)
            if cached is not None:
                tag, digest = cached["tag"], cached["digest"]
                print(f"resolve_runner_version: lost race, returning {tag}@{digest}", file=sys.stderr)
                return f"{IMAGE_REPO}:{tag}@{digest}"

        new_history = update_history(history, sha, tag, digest, now_utc())
        try:
            write_history(client, new_history, cm_exists, rv)
            print(f"resolve_runner_version: wrote ConfigMap, entries={len(new_history)}", file=sys.stderr)
            return f"{IMAGE_REPO}:{tag}@{digest}"
        except ApiError as e:
            if getattr(e.status, "code", None) == 409 and attempt < MAX_WRITE_ATTEMPTS - 1:
                print(
                    f"resolve_runner_version: write conflict (attempt {attempt + 1}), retrying",
                    file=sys.stderr,
                )
                continue
            raise

    raise RuntimeError(f"write retries exhausted after {MAX_WRITE_ATTEMPTS} attempts")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: resolve_runner_version.py <cluster-id>", file=sys.stderr)
        return 2
    cluster_id = argv[1]

    try:
        client = build_client()
        image_ref = _run(cluster_id, client)
    except requests.RequestException as e:
        print(f"resolve_runner_version: GitHub API request failed: {e}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or "").strip()
        print(f"resolve_runner_version: subprocess failed ({e.cmd[0]}): {stderr or e}", file=sys.stderr)
        return 1
    except json.JSONDecodeError as e:
        print(f"resolve_runner_version: ConfigMap history.json is not valid JSON: {e}", file=sys.stderr)
        return 1
    except ApiError as e:
        print(f"resolve_runner_version: kubernetes API error: {e}", file=sys.stderr)
        return 1
    except ValueError as e:
        print(f"resolve_runner_version: {e}", file=sys.stderr)
        return 1

    print(image_ref)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
