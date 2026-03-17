"""Smoke tests for ARC runner scale sets.

Validates that runner definitions are well-formed and that the expected
ConfigMaps + Helm releases exist in the cluster for each definition.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml
from helpers import find_helm_release, run_kubectl

pytestmark = [pytest.mark.live]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

NAMESPACE = "arc-runners"
MODULE_LABEL = "osdc.io/module=arc-runners"
REQUIRED_FIELDS = {"name", "instance_type", "disk_size", "vcpu", "memory"}


def _normalize_name(name: str) -> str:
    return name.replace(".", "-").replace("_", "-")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _defs_dir(upstream_dir: Path) -> Path:
    """Resolve the runner definitions directory, respecting env override."""
    override = os.environ.get("ARC_RUNNERS_DEFS_DIR")
    if override:
        return Path(override)
    return upstream_dir / "modules" / "arc-runners" / "defs"


def _load_all_defs(upstream_dir: Path) -> list[dict]:
    """Load all runner definition YAML files and return the runner dicts."""
    defs_path = _defs_dir(upstream_dir)
    defs = []
    for f in sorted(defs_path.glob("*.yaml")):
        data = yaml.safe_load(f.read_text())
        if data and "runner" in data:
            defs.append(data["runner"])
    return defs


# ============================================================================
# Offline: Runner Definition Validation
# ============================================================================


class TestRunnerDefs:
    """Validate runner definition files are well-formed."""

    def test_defs_exist(self, upstream_dir: Path) -> None:
        """At least one runner definition exists."""
        defs = _load_all_defs(upstream_dir)
        assert len(defs) > 0, "No runner definitions found"

    def test_required_fields(self, upstream_dir: Path) -> None:
        """Every runner def has the required fields."""
        defs = _load_all_defs(upstream_dir)
        for d in defs:
            missing = REQUIRED_FIELDS - set(d.keys())
            assert not missing, f"Runner '{d.get('name', '?')}' missing fields: {missing}"

    def test_names_are_unique(self, upstream_dir: Path) -> None:
        """No duplicate runner names."""
        defs = _load_all_defs(upstream_dir)
        names = [d["name"] for d in defs]
        dupes = [n for n in names if names.count(n) > 1]
        assert not dupes, f"Duplicate runner names: {set(dupes)}"


# ============================================================================
# Live: Runner ConfigMaps
# ============================================================================


class TestRunnerConfigMaps:
    """Verify ConfigMaps exist for each runner definition."""

    def test_configmaps_for_all_defs(self, upstream_dir: Path, enabled_modules: list[str]) -> None:
        """Each runner def has a matching ConfigMap in arc-runners namespace."""
        if "arc-runners" not in enabled_modules:
            pytest.skip("arc-runners module not enabled")

        defs = _load_all_defs(upstream_dir)
        result = run_kubectl(["get", "configmaps", "-l", MODULE_LABEL, "-o", "json"], namespace=NAMESPACE)
        cm_names = {item["metadata"]["name"] for item in result.get("items", [])}

        missing = []
        for d in defs:
            expected = f"arc-runner-hook-{_normalize_name(d['name'])}"
            if expected not in cm_names:
                missing.append(expected)

        assert not missing, f"Missing ConfigMaps for runner defs: {missing}"


# ============================================================================
# Live: Runner Helm Releases
# ============================================================================


class TestRunnerHelmReleases:
    """Verify Helm releases exist for each runner definition."""

    def test_helm_releases_for_all_defs(
        self, upstream_dir: Path, all_helm_releases: list[dict], enabled_modules: list[str]
    ) -> None:
        """Each runner def has a matching Helm release."""
        if "arc-runners" not in enabled_modules:
            pytest.skip("arc-runners module not enabled")

        defs = _load_all_defs(upstream_dir)
        missing = []
        for d in defs:
            release_name = f"arc-{_normalize_name(d['name'])}"
            release = find_helm_release(all_helm_releases, release_name)
            if release is None:
                missing.append(release_name)

        assert not missing, f"Missing Helm releases for runner defs: {missing}"


# ============================================================================
# Live: No Stale Runners
# ============================================================================


class TestNoStaleRunners:
    """Verify no orphaned ConfigMaps exist that don't match any runner def."""

    def test_no_stale_configmaps(self, upstream_dir: Path, enabled_modules: list[str]) -> None:
        """All ConfigMaps with the arc-runners label match a known runner def."""
        if "arc-runners" not in enabled_modules:
            pytest.skip("arc-runners module not enabled")

        defs = _load_all_defs(upstream_dir)
        expected_cms = {f"arc-runner-hook-{_normalize_name(d['name'])}" for d in defs}

        result = run_kubectl(["get", "configmaps", "-l", MODULE_LABEL, "-o", "json"], namespace=NAMESPACE)
        actual_cms = {item["metadata"]["name"] for item in result.get("items", [])}

        stale = actual_cms - expected_cms
        assert not stale, f"Stale ConfigMaps (no matching def): {stale}"
