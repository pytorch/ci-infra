"""Shared parsers for nodepool def YAMLs.

Used by:
- ``modules/nodepools/scripts/python/generate_nodepools.py`` (the canonical
  consumer — generates NodePool manifests from these defs)
- ``modules/arc-runners/scripts/python/generate_runners.py`` (filters runners
  whose backing nodepool is excluded for the current region)
- ``scripts/python/runner_fleet_validator.py`` (cross-checks runner defs
  against the fleet names produced by these defs)

A def YAML uses exactly one of three shapes — ``fleet`` (single fleet),
``fleets`` (list of fleets), or legacy ``nodepool`` (single instance type).
The helpers here parse each shape uniformly.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from pathlib import Path


def is_excluded_for_region(def_data: dict, region: str) -> bool:
    """Return True if ``region`` appears in the def's ``exclude_regions`` list.

    Returns False when ``region`` is empty/None or the def has no
    ``exclude_regions`` key — keeps callers backward-compatible when region is
    not supplied.
    """
    if not region:
        return False
    if not isinstance(def_data, dict):
        return False
    return region in (def_data.get("exclude_regions") or [])


def load_excluded_instance_types(defs_dir: Path, region: str) -> set[str]:
    """Return instance_types whose backing fleet/nodepool excludes ``region``.

    Reads every ``*.yaml`` under ``defs_dir``. Supports the ``fleet``,
    ``fleets``, and legacy ``nodepool`` shapes. An instance_type is excluded
    when its containing def has ``region`` in ``exclude_regions``. Returns
    empty set when ``region`` is falsy or the directory is missing.
    """
    excluded: set[str] = set()
    if not region or not defs_dir.is_dir():
        return excluded
    for def_file in sorted(defs_dir.glob("*.yaml")):
        try:
            data = yaml.safe_load(def_file.read_text()) or {}
        except yaml.YAMLError as e:
            print(f"warning: nodepool def {def_file}: YAML parse error: {e}", file=sys.stderr)
            continue
        if not isinstance(data, dict):
            continue
        if isinstance(data.get("fleet"), dict):
            _collect_excluded_from_fleet(data["fleet"], region, excluded)
        if isinstance(data.get("fleets"), list):
            for fleet in data["fleets"]:
                if isinstance(fleet, dict):
                    _collect_excluded_from_fleet(fleet, region, excluded)
        if isinstance(data.get("nodepool"), dict):
            nodepool = data["nodepool"]
            if is_excluded_for_region(nodepool, region):
                instance_type = nodepool.get("instance_type")
                if isinstance(instance_type, str) and instance_type:
                    excluded.add(instance_type)
    return excluded


def _collect_excluded_from_fleet(fleet: dict, region: str, sink: set[str]) -> None:
    if not is_excluded_for_region(fleet, region):
        return
    for inst in fleet.get("instances") or []:
        if isinstance(inst, dict):
            t = inst.get("type")
            if isinstance(t, str) and t:
                sink.add(t)


def iter_fleet_names(def_data: dict, region: str) -> list[str]:
    """Return the fleet names a single nodepool def contributes for ``region``.

    Handles all three shapes (``fleet``, ``fleets``, legacy ``nodepool``).
    Region-excluded entries contribute nothing. A def containing multiple
    shapes contributes the union of all shape-specific names — defensive
    against malformed defs that mix shapes.

    Legacy ``nodepool`` defs derive the fleet name from the instance family
    prefix (``c7i.48xlarge`` → ``c7i``); matches ``_process_nodepool`` in
    generate_nodepools.py.
    """
    if not isinstance(def_data, dict):
        return []
    names: list[str] = []

    fleet = def_data.get("fleet")
    if isinstance(fleet, dict) and not is_excluded_for_region(fleet, region):
        name = fleet.get("name")
        if isinstance(name, str) and name:
            names.append(name)

    fleets = def_data.get("fleets")
    if isinstance(fleets, list):
        for entry in fleets:
            if not isinstance(entry, dict):
                continue
            if is_excluded_for_region(entry, region):
                continue
            name = entry.get("name")
            if isinstance(name, str) and name:
                names.append(name)

    nodepool = def_data.get("nodepool")
    if isinstance(nodepool, dict) and not is_excluded_for_region(nodepool, region):
        instance_type = nodepool.get("instance_type")
        if isinstance(instance_type, str) and instance_type:
            names.append(instance_type.split(".")[0])

    return names
