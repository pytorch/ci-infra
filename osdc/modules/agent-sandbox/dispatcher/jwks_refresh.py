#!/usr/bin/env python3
"""Fetch GitHub's OIDC signing keys and write them into the ConfigMap the dispatcher reads.

Runs as a CronJob, NOT inside the dispatcher. That separation is the point: the
dispatcher is the process holding create-Job RBAC, so it is the one that must not be
able to reach the internet, and its NetworkPolicy allows DNS and the Kubernetes API and
nothing else. This job is allowed out to github.com and holds no RBAC beyond patching
one named ConfigMap — because write access to that object is the power to choose which
signing keys the dispatcher will trust.

Same image as the dispatcher, different command and a different service account.

Failure is silence, deliberately: if the fetch fails or returns something that is not a
usable key set, this exits non-zero WITHOUT writing, so the last known good keys stay in
place. The dispatcher's own staleness bound is what stops that being indefinite — it
refuses every token once the recorded fetch time is old enough, which turns a refresher
that has been failing for a day into a loud outage rather than a quiet drift into
trusting frozen key material.
"""

from __future__ import annotations

import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request

import kube
from jwt import PyJWKSet, PyJWKSetError

JWKS_URL = os.environ.get("JWKS_URL", "https://token.actions.githubusercontent.com/.well-known/jwks")
CONFIGMAP_NAME = os.environ.get("JWKS_CONFIGMAP", "oidc-jwks")
CONFIGMAP_KEY = "jwks.json"
FETCH_TIMEOUT_S = 20
MAX_JWKS_BYTES = 256 * 1024


def fetch_jwks() -> dict:
    """The published key set, or raise. Validated before it is allowed to be a result."""
    request = urllib.request.Request(JWKS_URL, headers={"Accept": "application/json"})  # noqa: S310
    with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT_S, context=ssl.create_default_context()) as response:  # noqa: S310
        body = response.read(MAX_JWKS_BYTES)

    document = json.loads(body)
    keys = document.get("keys")
    if not isinstance(keys, list) or not keys:
        raise ValueError("JWKS response contained no keys")
    if not all(isinstance(k, dict) and k.get("kid") for k in keys):
        raise ValueError("JWKS response contained a key with no kid")

    # Parsed with the SAME library the dispatcher will use, not merely shape-checked.
    # "a non-empty list of dicts with a kid" is not the property that matters — the
    # property that matters is that the dispatcher can load it, and anything else
    # overwrites a working key set with one that breaks at the next reload.
    # PyJWKSet raises when every key is unusable, so there is no emptiness check after
    # this: adding one would be unreachable code claiming to be a safety net.
    PyJWKSet.from_dict({"keys": keys})
    return {"keys": keys}


def write_configmap(jwks: dict) -> None:
    """Replace the ConfigMap's data in one PATCH.

    fetched_at is written INTO the document rather than left to the object's own
    metadata, because the dispatcher reads a mounted file and a ConfigMap volume only
    updates when its content changes. GitHub rotates rarely, so without a timestamp in
    the content, a successful refresh of unchanged keys would be invisible to the
    dispatcher and its staleness bound would eventually refuse a perfectly good key set.
    """
    document = json.dumps({"fetched_at": time.time(), "jwks": jwks}, sort_keys=True)
    kube.api_request(
        "PATCH",
        f"/api/v1/namespaces/{kube.NAMESPACE}/configmaps/{CONFIGMAP_NAME}",
        body={"data": {CONFIGMAP_KEY: document}},
        content_type="application/merge-patch+json",
    )


def main() -> int:
    try:
        jwks = fetch_jwks()
    except (urllib.error.URLError, OSError, ValueError, PyJWKSetError) as exc:
        print(f"[jwks-refresh] fetch failed, leaving the existing keys in place: {exc}", file=sys.stderr)
        return 1
    try:
        write_configmap(jwks)
    except (kube.ApiError, OSError) as exc:
        print(f"[jwks-refresh] could not update {CONFIGMAP_NAME}: {exc}", file=sys.stderr)
        return 1
    print(f"[jwks-refresh] wrote {len(jwks['keys'])} signing keys to {CONFIGMAP_NAME}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
