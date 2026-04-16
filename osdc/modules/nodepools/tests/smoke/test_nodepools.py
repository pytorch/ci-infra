"""Smoke tests for Karpenter NodePool definitions.

Validates that NodePool definition files are well-formed (offline) and that
the expected NodePool CRs exist in the cluster with no stale leftovers (live).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

pytestmark = [pytest.mark.live]

REQUIRED_FIELDS = {"name", "instance_type", "arch", "node_disk_size", "gpu"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_defs_dir(upstream_dir: Path) -> Path:
    """Get the nodepools defs directory (respects consumer override)."""
    override = os.environ.get("NODEPOOLS_DEFS_DIR")
    if override:
        return Path(override)
    return upstream_dir / "modules" / "nodepools" / "defs"


def _load_all_defs(upstream_dir: Path) -> list[dict]:
    """Load all nodepool definition YAML files and return the nodepool dicts.

    Supports three formats: ``nodepool:`` (legacy), ``fleet:`` (single fleet),
    and ``fleets:`` (multi-fleet per file, e.g. GPU families).
    """
    defs_dir = _get_defs_dir(upstream_dir)
    defs = []
    for f in sorted(defs_dir.glob("*.yaml")):
        data = yaml.safe_load(f.read_text())
        if not data:
            continue
        if "nodepool" in data:
            defs.append(data["nodepool"])
        elif "fleet" in data:
            defs.extend(_expand_fleet(data["fleet"]))
        elif "fleets" in data:
            for fleet_data in data["fleets"]:
                defs.extend(_expand_fleet(fleet_data))
    return defs


def _expand_fleet(fleet_data: dict) -> list[dict]:
    """Expand a fleet definition into individual nodepool-like dicts for validation."""
    result = []
    for inst in fleet_data.get("instances", []):
        result.append(
            {
                "name": inst["type"].replace(".", "-"),
                "instance_type": inst["type"],
                "arch": fleet_data["arch"],
                "gpu": fleet_data.get("gpu", False),
                "node_disk_size": inst["node_disk_size"],
            }
        )
    for inst in fleet_data.get("release", []):
        result.append(
            {
                "name": f"{inst['type'].replace('.', '-')}-release",
                "instance_type": inst["type"],
                "arch": fleet_data["arch"],
                "gpu": fleet_data.get("gpu", False),
                "node_disk_size": inst["node_disk_size"],
            }
        )
    return result


# ============================================================================
# Offline: Definition Validation
# ============================================================================


@pytest.mark.offline
class TestNodePoolDefs:
    """Validate nodepool definition files are well-formed."""

    def test_defs_exist(self, upstream_dir: Path) -> None:
        """At least one nodepool definition exists."""
        defs = _load_all_defs(upstream_dir)
        assert len(defs) > 0, "No nodepool definitions found"

    def test_required_fields(self, upstream_dir: Path) -> None:
        """Every nodepool def has the required fields."""
        defs = _load_all_defs(upstream_dir)
        for d in defs:
            missing = REQUIRED_FIELDS - set(d.keys())
            assert not missing, f"NodePool '{d.get('name', '?')}' missing fields: {missing}"

    def test_names_are_unique(self, upstream_dir: Path) -> None:
        """No duplicate nodepool names."""
        defs = _load_all_defs(upstream_dir)
        names = [d["name"] for d in defs]
        dupes = [n for n in names if names.count(n) > 1]
        assert not dupes, f"Duplicate nodepool names: {set(dupes)}"

    def test_arch_values(self, upstream_dir: Path) -> None:
        """Arch must be amd64 or arm64."""
        defs = _load_all_defs(upstream_dir)
        for d in defs:
            assert d["arch"] in ("amd64", "arm64"), (
                f"NodePool '{d['name']}' has invalid arch '{d['arch']}' (expected amd64 or arm64)"
            )

    def test_gpu_is_bool(self, upstream_dir: Path) -> None:
        """GPU field must be a boolean."""
        defs = _load_all_defs(upstream_dir)
        for d in defs:
            assert isinstance(d["gpu"], bool), (
                f"NodePool '{d['name']}' gpu field is {type(d['gpu']).__name__}, expected bool"
            )


# ============================================================================
# Live: NodePool CRs exist
# ============================================================================


class TestNodePoolCRs:
    """Verify Karpenter NodePool CRs exist for each definition."""

    def test_nodepools_exist(self, all_nodepools: dict, upstream_dir: Path) -> None:
        """Each nodepool def has a matching NodePool CR in the cluster."""
        defs = _load_all_defs(upstream_dir)
        existing = {np["metadata"]["name"] for np in all_nodepools.get("items", [])}
        missing = [d["name"] for d in defs if d["name"] not in existing]
        assert not missing, f"NodePool CRs not found for definitions: {missing}"


# ============================================================================
# Live: No stale NodePools
# ============================================================================


class TestNoStaleNodePools:
    """Verify no orphaned NodePools exist that don't match any definition."""

    def test_no_stale_nodepools(self, all_nodepools: dict, upstream_dir: Path) -> None:
        """All NodePools with the nodepools module label match a known def."""
        defs = _load_all_defs(upstream_dir)
        expected = {d["name"] for d in defs}
        managed = [
            np
            for np in all_nodepools.get("items", [])
            if np.get("metadata", {}).get("labels", {}).get("osdc.io/module") == "nodepools"
        ]
        stale = [np["metadata"]["name"] for np in managed if np["metadata"]["name"] not in expected]
        assert not stale, f"Stale NodePools (managed but no matching def): {stale}"
