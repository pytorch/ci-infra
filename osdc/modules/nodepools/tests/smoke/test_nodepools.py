"""Smoke tests for Karpenter NodePool definitions.

Validates that NodePool definition files are well-formed (offline) and that
the expected NodePool CRs exist in the cluster with no stale leftovers (live).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
import yaml

# Import shared helpers from the generator so smoke tests and generator agree
# on naming (e.g. fleet name vs instance family disambiguation) and on region
# exclusion logic (so excluded fleets are skipped consistently).
_GEN_DIR = Path(__file__).resolve().parents[2] / "scripts" / "python"
if str(_GEN_DIR) not in sys.path:
    sys.path.insert(0, str(_GEN_DIR))
from generate_nodepools import _fleet_nodepool_name, _is_excluded_for_region  # noqa: E402

pytestmark = [pytest.mark.live]

REQUIRED_FIELDS = {"name", "instance_type", "arch", "node_disk_size", "gpu"}


def _expected_azs(cluster_config: dict) -> list[str]:
    """Sorted AZ list expected for this cluster's NodePools.

    Source-of-truth matches what ``deploy.sh`` passes to the generator:
    the AZ keys defined under ``base.pod_cidr_buckets`` in clusters.yaml
    (see ``scripts/cluster-config.py azs``). The generator emits one
    NodePool per (def, AZ) pair, so the expected CR set fans out the same way.
    """
    base_cfg = cluster_config["cluster"].get("base") or {}
    buckets = base_cfg.get("pod_cidr_buckets") or {}
    if not buckets:
        pytest.skip("This cluster has no pod_cidr_buckets configured (AZ pinning not in effect)")
    azs: set[str] = set()
    for az_map in buckets.values():
        azs.update(az_map.keys())
    return sorted(azs)


def _expand_for_azs(base_names: set[str], azs: list[str]) -> set[str]:
    """Cartesian-expand base NodePool names with AZ suffixes."""
    return {f"{name}-{az}" for name in base_names for az in azs}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_defs_dir(upstream_dir: Path) -> Path:
    """Get the nodepools defs directory (respects consumer override)."""
    override = os.environ.get("NODEPOOLS_DEFS_DIR")
    if override:
        return Path(override)
    return upstream_dir / "modules" / "nodepools" / "defs"


def _load_all_defs(upstream_dir: Path, region: str | None = None) -> list[dict]:
    """Load all nodepool definition YAML files and return the nodepool dicts.

    Supports three formats: ``nodepool:`` (legacy), ``fleet:`` (single fleet),
    and ``fleets:`` (multi-fleet per file, e.g. GPU families).

    When ``region`` is provided, fleet/nodepool defs whose ``exclude_regions``
    list contains that region are skipped — mirroring the generator's
    ``_is_excluded_for_region`` behavior so the expected NodePool set matches
    what was actually rendered for the cluster. When ``region`` is None
    (offline def-validation tests), all defs are returned regardless.
    """
    defs_dir = _get_defs_dir(upstream_dir)
    defs = []
    for f in sorted(defs_dir.glob("*.yaml")):
        data = yaml.safe_load(f.read_text())
        if not data:
            continue
        if "nodepool" in data:
            if _is_excluded_for_region(data["nodepool"], region):
                continue
            defs.append(data["nodepool"])
        elif "fleet" in data:
            if _is_excluded_for_region(data["fleet"], region):
                continue
            defs.extend(_expand_fleet(data["fleet"]))
        elif "fleets" in data:
            for fleet_data in data["fleets"]:
                if _is_excluded_for_region(fleet_data, region):
                    continue
                defs.extend(_expand_fleet(fleet_data))
    return defs


def _expand_fleet(fleet_data: dict) -> list[dict]:
    """Expand a fleet definition into individual nodepool-like dicts for validation."""
    result = []
    fleet_name = fleet_data["name"]
    for inst in fleet_data.get("instances", []):
        result.append(
            {
                "name": _fleet_nodepool_name(fleet_name, inst["type"]),
                "instance_type": inst["type"],
                "arch": fleet_data["arch"],
                "gpu": fleet_data.get("gpu", False),
                "node_disk_size": inst["node_disk_size"],
            }
        )
    for inst in fleet_data.get("release", []):
        result.append(
            {
                "name": _fleet_nodepool_name(fleet_name, inst["type"], name_suffix="-release"),
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

    def test_nodepools_exist(self, all_nodepools: dict, upstream_dir: Path, cluster_config: dict) -> None:
        """Each (def, AZ) pair has a matching NodePool CR in the cluster.

        The generator emits one NodePool per (def, AZ), where AZs come from
        ``base.pod_cidr_buckets`` in clusters.yaml. The expected CR name set
        is therefore the cartesian product of base def names and configured
        AZs. ``exclude_regions`` is honored so fleets that the generator
        correctly skipped for this cluster's region are not asserted.
        """
        region = cluster_config["cluster"].get("region", "")
        defs = _load_all_defs(upstream_dir, region=region)
        azs = _expected_azs(cluster_config)
        expected = _expand_for_azs({d["name"] for d in defs}, azs)
        existing = {np["metadata"]["name"] for np in all_nodepools.get("items", [])}
        missing = sorted(expected - existing)
        assert not missing, f"NodePool CRs not found for (def, AZ) pairs: {missing}"


# ============================================================================
# Live: No stale NodePools
# ============================================================================


class TestNoStaleNodePools:
    """Verify no orphaned NodePools exist that don't match any definition."""

    def test_no_stale_nodepools(self, all_nodepools: dict, upstream_dir: Path, cluster_config: dict) -> None:
        """All NodePools with the nodepools module label match a known (def, AZ) pair.

        Every NodePool name is ``<def-name>-<AZ>``. A CR whose suffix doesn't
        correspond to a configured AZ (e.g. left over from a previous deploy
        that targeted a different AZ set) is flagged as stale rather than
        masked as "expected". ``exclude_regions`` is honored so a leftover CR
        from a region-excluded def is also flagged.
        """
        region = cluster_config["cluster"].get("region", "")
        defs = _load_all_defs(upstream_dir, region=region)
        azs = _expected_azs(cluster_config)
        expected = _expand_for_azs({d["name"] for d in defs}, azs)
        managed = [
            np
            for np in all_nodepools.get("items", [])
            if np.get("metadata", {}).get("labels", {}).get("osdc.io/module") == "nodepools"
        ]
        stale = sorted(np["metadata"]["name"] for np in managed if np["metadata"]["name"] not in expected)
        assert not stale, f"Stale NodePools (managed but no matching (def, AZ) pair): {stale}"
