"""Smoke tests for the deploy-time runner-image resolver.

Verifies the `arc-runner-version-lock` ConfigMap (written by
`resolve_runner_version.py` during `just deploy-module <cluster> arc-runners`)
matches the runner image actually deployed in the cluster's
AutoscalingRunnerSets.

Skipped on clusters that pin `arc.runner_image_tag` in clusters.yaml
(rollback path — the resolver is bypassed and no ConfigMap is written).
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime

import pytest
from helpers import retry_with_backoff, run_kubectl

pytestmark = [pytest.mark.live]

LOCK_CM_NAME = "arc-runner-version-lock"
LOCK_CM_NAMESPACE = "osdc-system"
LOCK_CM_KEY = "history.json"
HISTORY_MAX = 20

EXPECTED_LABELS = {
    "app.kubernetes.io/managed-by": "osdc-deploy-log",
    "osdc.io/lock-kind": "arc-runner-version",
}

IMAGE_REPO = "ghcr.io/actions/actions-runner"
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

ARS_RESOURCE = "autoscalingrunnersets.actions.github.com"
ARS_NAMESPACE = "arc-runners"
RUNNER_CONTAINER_NAME = "runner"


def _parse_iso8601_utc(value: str) -> datetime:
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value)


def _fetch_lock_configmap() -> dict:
    return run_kubectl(["get", "configmap", LOCK_CM_NAME], namespace=LOCK_CM_NAMESPACE)


def _fetch_arc_runner_sets() -> list[dict]:
    result = run_kubectl(["get", ARS_RESOURCE], namespace=ARS_NAMESPACE)
    return result.get("items", []) or []


def _runner_image_from_ars(ars: dict) -> str | None:
    containers = ars.get("spec", {}).get("template", {}).get("spec", {}).get("containers", []) or []
    for c in containers:
        if c.get("name") == RUNNER_CONTAINER_NAME:
            return c.get("image")
    return None


@pytest.fixture(scope="module")
def explicit_runner_tag(resolve_config) -> str | None:
    val = resolve_config("arc.runner_image_tag")
    if val is None or val == "":
        return None
    return str(val)


@pytest.fixture(scope="module")
def lock_history(explicit_runner_tag: str | None) -> list[dict]:
    if explicit_runner_tag is not None:
        pytest.skip(
            f"arc.runner_image_tag={explicit_runner_tag!r} pinned in clusters.yaml — "
            f"resolver bypassed, no {LOCK_CM_NAME} ConfigMap expected"
        )

    def _load() -> tuple[dict, list]:
        cm = _fetch_lock_configmap()
        raw = (cm.get("data") or {}).get(LOCK_CM_KEY)
        assert raw, f"{LOCK_CM_NAME}.data[{LOCK_CM_KEY!r}] is empty or missing"
        parsed = json.loads(raw)
        assert isinstance(parsed, list), f"{LOCK_CM_KEY} must be a JSON list, got {type(parsed).__name__}"
        assert len(parsed) >= 1, f"{LOCK_CM_KEY} has zero entries — at least one expected after a deploy"
        return cm, parsed

    try:
        cm, history = retry_with_backoff(_load)
    except subprocess.CalledProcessError as e:
        pytest.fail(
            f"Failed to read ConfigMap {LOCK_CM_NAMESPACE}/{LOCK_CM_NAME}: {e.stderr or e}. "
            f"If this cluster has never deployed arc-runners after the resolver landed, "
            f"run `just deploy-module {{cluster}} arc-runners` first."
        )

    labels = cm.get("metadata", {}).get("labels", {}) or {}
    for k, v in EXPECTED_LABELS.items():
        assert labels.get(k) == v, f"{LOCK_CM_NAME} label {k!r}={labels.get(k)!r}, expected {v!r}"

    return history


class TestLockConfigMapShape:
    def test_history_length_within_cap(self, lock_history: list[dict]) -> None:
        assert len(lock_history) <= HISTORY_MAX, f"history.json has {len(lock_history)} entries, cap is {HISTORY_MAX}"

    def test_history_entries_well_formed(self, lock_history: list[dict]) -> None:
        problems: list[str] = []
        for i, entry in enumerate(lock_history):
            if not isinstance(entry, dict):
                problems.append(f"[{i}] not an object: {entry!r}")
                continue

            tag = entry.get("tag")
            if not isinstance(tag, str) or not tag:
                problems.append(f"[{i}] tag must be a non-empty string, got {tag!r}")

            digest = entry.get("digest")
            if not isinstance(digest, str) or not DIGEST_RE.match(digest):
                problems.append(f"[{i}] digest must match sha256:<64-hex>, got {digest!r}")

            resolved_at = entry.get("resolved_at")
            if not isinstance(resolved_at, str):
                problems.append(f"[{i}] resolved_at must be a string, got {resolved_at!r}")
            else:
                try:
                    parsed = _parse_iso8601_utc(resolved_at)
                except ValueError as e:
                    problems.append(f"[{i}] resolved_at not ISO8601: {resolved_at!r} ({e})")
                else:
                    if parsed.tzinfo is None:
                        problems.append(f"[{i}] resolved_at missing tz info: {resolved_at!r}")

        assert not problems, "Malformed history entries:\n" + "\n".join(problems)

    def test_tags_are_unique(self, lock_history: list[dict]) -> None:
        tags = [e["tag"] for e in lock_history if isinstance(e, dict) and isinstance(e.get("tag"), str)]
        dupes = sorted({t for t in tags if tags.count(t) > 1})
        assert not dupes, f"Duplicate tags in history (resolver should dedupe): {dupes}"


class TestLockMatchesDeployedRunners:
    def test_newest_entry_matches_ars_runner_image(
        self,
        lock_history: list[dict],
        enabled_modules: list[str],
    ) -> None:
        if "arc-runners" not in enabled_modules:
            pytest.skip("arc-runners module not enabled")

        newest = lock_history[0]
        expected_image = f"{IMAGE_REPO}:{newest['tag']}@{newest['digest']}"

        try:
            ars_list = retry_with_backoff(_fetch_arc_runner_sets)
        except subprocess.CalledProcessError as e:
            pytest.fail(f"Failed to list {ARS_RESOURCE} in {ARS_NAMESPACE}: {e.stderr or e}")

        assert len(ars_list) >= 1, (
            f"No {ARS_RESOURCE} found in '{ARS_NAMESPACE}' — arc-runners module enabled but no scale-sets deployed?"
        )

        mismatches: list[str] = []
        for ars in ars_list:
            name = ars.get("metadata", {}).get("name", "?")
            image = _runner_image_from_ars(ars)
            if image is None:
                mismatches.append(f"{name}: no '{RUNNER_CONTAINER_NAME}' container in pod template")
                continue
            if image != expected_image:
                mismatches.append(f"{name}: image={image!r}, expected {expected_image!r}")

        assert not mismatches, (
            f"AutoscalingRunnerSet runner image does not match newest lock entry "
            f"({expected_image!r}):\n" + "\n".join(mismatches)
        )
