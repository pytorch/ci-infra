"""Helpers for loading ARC runner definitions and mapping live pods to defs.

These helpers are shared between the listener env-var coherence test and the
placeholder ↔ workflow scheduling parity test.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

# Label that ARC sets on listener pods. Value = AutoscalingRunnerSet's name,
# which is the chart's `runnerScaleSetName` = `<runner_name_prefix><def_name>`.
SCALE_SET_NAME_LABEL = "actions.github.com/scale-set-name"


def defs_dir(upstream_dir: Path) -> Path:
    """Resolve the runner definitions directory, respecting env override."""
    override = os.environ.get("ARC_RUNNERS_DEFS_DIR")
    if override:
        return Path(override)
    return upstream_dir / "modules" / "arc-runners" / "defs"


def load_runner_defs(upstream_dir: Path) -> list[dict]:
    """Load all runner definition YAML files; return the inner `runner` dicts."""
    out: list[dict] = []
    for f in sorted(defs_dir(upstream_dir).glob("*.yaml")):
        data = yaml.safe_load(f.read_text())
        if data and "runner" in data:
            out.append(data["runner"])
    return out


def load_defs_by_name(upstream_dir: Path) -> dict[str, dict]:
    """Index runner defs by `name` for fast lookup."""
    return {d["name"]: d for d in load_runner_defs(upstream_dir)}


def def_name_from_scale_set(scale_set_name: str, runner_name_prefix: str) -> str | None:
    """Strip the cluster's runner_name_prefix from a scale-set name.

    Returns None if the name does not start with the prefix (which would
    indicate a stale scale-set or a misconfigured cluster — surface that
    rather than silently returning the unstripped name).

    Example:
        scale_set_name = "c-mt-l-arm64g2-6-32"
        runner_name_prefix = "c-mt-"
        returns: "l-arm64g2-6-32"

    An empty prefix is allowed — the scale-set name is the def name as-is.
    """
    if runner_name_prefix and not scale_set_name.startswith(runner_name_prefix):
        return None
    return scale_set_name[len(runner_name_prefix) :]


def def_for_listener_pod(
    pod: dict,
    defs_by_name: dict[str, dict],
    runner_name_prefix: str,
) -> tuple[str | None, dict | None]:
    """Map a listener pod to its runner def via the scale-set-name label.

    Returns ``(def_name, def_dict)`` or ``(None, None)`` if no mapping.
    The def_name is returned even when the def is missing (for diagnostics).
    """
    labels = pod.get("metadata", {}).get("labels", {})
    scale_set = labels.get(SCALE_SET_NAME_LABEL)
    if not scale_set:
        return None, None
    def_name = def_name_from_scale_set(scale_set, runner_name_prefix)
    if def_name is None:
        return None, None
    return def_name, defs_by_name.get(def_name)
