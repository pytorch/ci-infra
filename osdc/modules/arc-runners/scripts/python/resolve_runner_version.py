#!/usr/bin/env python3
"""Resolve the actions/runner image to a digest-pinned reference.

Calls GitHub's /releases/latest, caches the (tag, digest) pair in a
ConfigMap (`arc-runner-version-lock` in `osdc-system`), and prints
`ghcr.io/actions/actions-runner:<tag>@<digest>` to stdout.

Operator override: set `arc.runner_image_tag` in `clusters.yaml`; the
caller bypasses this script entirely in that case.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime

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

REQUEST_TIMEOUT_SECONDS = 10


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


def read_history(client: Client) -> tuple[list[dict[str, str]], bool]:
    try:
        cm = client.get(ConfigMap, name=CM_NAME, namespace=CM_NAMESPACE)
    except ApiError as e:
        if getattr(e.status, "code", None) == 404:
            return [], False
        raise
    raw = (cm.data or {}).get(CM_KEY)
    if raw is None:
        return [], True
    parsed = json.loads(raw)
    if not isinstance(parsed, list):
        raise ValueError(f"{CM_KEY} must contain a JSON list")
    for entry in parsed:
        if not isinstance(entry, dict) or "tag" not in entry or "digest" not in entry:
            raise ValueError(f"{CM_KEY} entries must be objects with 'tag' and 'digest'")
    return parsed, True


def find_cached_digest(history: list[dict[str, str]], tag: str) -> str | None:
    for entry in history:
        if entry.get("tag") == tag:
            return entry.get("digest")
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
    tag: str,
    digest: str,
    now: datetime,
) -> list[dict[str, str]]:
    new_entry = {
        "tag": tag,
        "digest": digest,
        "resolved_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    kept = [e for e in history if e.get("tag") != tag]
    return [new_entry, *kept][:HISTORY_MAX]


def write_history(client: Client, history: list[dict[str, str]], cm_exists: bool) -> None:
    payload = json.dumps(history, indent=2)
    cm = ConfigMap(
        metadata=ObjectMeta(name=CM_NAME, namespace=CM_NAMESPACE, labels=CM_LABELS),
        data={CM_KEY: payload},
    )
    if cm_exists:
        client.replace(cm)
    else:
        client.create(cm)


def build_client() -> Client:
    return Client()


def now_utc() -> datetime:
    return datetime.now(UTC)


def _run(cluster_id: str, client: Client) -> str:
    print(f"resolve_runner_version: cluster={cluster_id}", file=sys.stderr)

    token = os.environ.get("GITHUB_TOKEN") or None
    tag = fetch_latest_tag(token)
    print(f"resolve_runner_version: latest release tag={tag}", file=sys.stderr)

    history, cm_exists = read_history(client)

    cached = find_cached_digest(history, tag)
    if cached is not None:
        print(f"resolve_runner_version: cache hit, digest={cached}", file=sys.stderr)
        return f"{IMAGE_REPO}:{tag}@{cached}"

    print("resolve_runner_version: cache miss, resolving digest via crane", file=sys.stderr)
    digest = resolve_digest(tag)
    print(f"resolve_runner_version: resolved digest={digest}", file=sys.stderr)

    new_history = update_history(history, tag, digest, now_utc())
    write_history(client, new_history, cm_exists)
    print(f"resolve_runner_version: wrote ConfigMap, entries={len(new_history)}", file=sys.stderr)

    return f"{IMAGE_REPO}:{tag}@{digest}"


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
        print(f"resolve_runner_version: crane digest failed: {stderr or e}", file=sys.stderr)
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
