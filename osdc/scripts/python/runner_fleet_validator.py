"""Validate that every arc-runner def references a node-fleet defined by the
cluster's enabled nodepool modules.

This catches the silent-mismatch failure mode where a runner def's ``node_fleet``
override (or its instance-family fallback) does not match any fleet name produced
by the cluster's nodepools modules — at apply time the workflow pod's node
affinity matches nothing and the job pends forever.

The validator walks every ``nodepools*`` module listed in the cluster's
``modules`` for the set of available fleets, walks every ``arc-runners*`` module
for the runner defs, and reports a precise error per orphan runner.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import yaml
from fleet_naming import RESERVED_NODE_FLEET_NAMES, derive_fleet_name
from nodepool_defs import iter_fleet_names, load_excluded_instance_types

if TYPE_CHECKING:
    from pathlib import Path


def _collect_excluded_instance_types(
    roots: list[Path],
    nodepool_modules: list[str],
    region: str,
) -> set[str]:
    excluded: set[str] = set()
    for root in roots:
        for module in nodepool_modules:
            defs_dir = root / "modules" / module / "defs"
            if not defs_dir.is_dir():
                continue
            excluded |= load_excluded_instance_types(defs_dir, region)
    return excluded


def _resolve_region(cluster_cfg: dict, defaults: dict) -> str:
    region = cluster_cfg.get("region")
    if region:
        return region
    return defaults.get("region", "")


def _enabled_modules(cluster_cfg: dict, prefix: str) -> list[str]:
    modules = cluster_cfg.get("modules") or []
    return [m for m in modules if isinstance(m, str) and (m == prefix or m.startswith(f"{prefix}-"))]


def _collect_available_fleets(
    roots: list[Path],
    nodepool_modules: list[str],
    region: str,
) -> tuple[set[str], list[str]]:
    """Return ``(available_fleets, collision_errors)``.

    Walks each root x module x def file. A fleet name defined by two different
    files (across modules or roots) is reported as a collision error — silent
    merge would mask configuration bugs. Reserved names (c7i-runner) are
    excluded from the available set even when defined, because workflow pods
    are forbidden from targeting them.
    """
    owners: dict[str, str] = {}
    errors: list[str] = []
    for root in roots:
        for module in nodepool_modules:
            defs_dir = root / "modules" / module / "defs"
            if not defs_dir.is_dir():
                continue
            for def_file in sorted(defs_dir.glob("*.yaml")):
                try:
                    data = yaml.safe_load(def_file.read_text()) or {}
                except yaml.YAMLError as e:
                    errors.append(f"nodepool def {def_file}: YAML parse error: {e}")
                    continue
                for name in iter_fleet_names(data, region):
                    origin = f"{module}/{def_file.name}"
                    prior = owners.get(name)
                    if prior is None:
                        owners[name] = origin
                    elif prior != origin:
                        errors.append(f"fleet name collision: '{name}' defined by both '{prior}' and '{origin}'")
    available = {name for name in owners if name not in RESERVED_NODE_FLEET_NAMES}
    return available, errors


def _collect_runner_defs(roots: list[Path], runner_modules: list[str]) -> list[tuple[Path, str]]:
    """Return ``(def_path, owning_module)`` for every runner def under roots.

    Consumer-fork files win on duplicate basename (same convention runner
    overhead loader uses): a later root entry replaces an earlier one. We walk
    roots in order and key by ``(module, basename)`` so the consumer override
    of ``arc-runners/defs/foo.yaml`` displaces the upstream one.
    """
    by_key: dict[tuple[str, str], tuple[Path, str]] = {}
    for root in roots:
        for module in runner_modules:
            defs_dir = root / "modules" / module / "defs"
            if not defs_dir.is_dir():
                continue
            for def_file in sorted(defs_dir.glob("*.yaml")):
                by_key[(module, def_file.name)] = (def_file, module)
    return sorted(by_key.values(), key=lambda pair: (pair[1], pair[0].name))


def _validate_one_runner(
    cluster_id: str,
    def_file: Path,
    module: str,
    available_fleets: set[str],
    excluded_instance_types: set[str],
) -> str | None:
    try:
        data = yaml.safe_load(def_file.read_text()) or {}
    except yaml.YAMLError as e:
        return f"cluster={cluster_id} module={module} def={def_file.name}: YAML parse error: {e}"

    runner = data.get("runner") or {}
    if not isinstance(runner, dict):
        return None
    name = runner.get("name")
    instance_type = runner.get("instance_type")
    if not name or not instance_type:
        return None

    # Runners whose instance_type is excluded for this region have max_runners=0
    # and proactive_capacity=0 forced by generate_runners.py — they're emitted
    # so YAMLs round-trip across regions but never receive jobs here.
    if instance_type in excluded_instance_types:
        return None

    override = runner.get("node_fleet")
    try:
        effective = derive_fleet_name(instance_type, override=override)
    except ValueError as e:
        return (
            f"cluster={cluster_id} runner={name} instance_type={instance_type} "
            f"node_fleet={override!r} — invalid override: {e}"
        )

    if effective in available_fleets:
        return None

    available_sorted = sorted(available_fleets)
    return (
        f"cluster={cluster_id} runner={name} instance_type={instance_type} "
        f"effective_fleet={effective} (override={override!r}) — no NodePool defines "
        f"this fleet in this cluster's enabled modules. Available: {available_sorted}. "
        f"Hint: if `node_fleet` is unset and the effective_fleet is the instance family "
        f"prefix, check the instance_type spelling."
    )


def validate_cluster_runner_fleets(
    cluster_id: str,
    clusters_yaml: dict,
    upstream_dir: Path,
    consumer_root: Path | None = None,
) -> list[str]:
    """Validate every arc-runner def in ``cluster_id`` against available fleets.

    Returns a list of human-readable error messages (empty list = pass).
    Skips clusters that don't list any ``arc-runners*`` module.
    """
    clusters = clusters_yaml.get("clusters") or {}
    cluster_cfg = clusters.get(cluster_id)
    if not isinstance(cluster_cfg, dict):
        return [f"cluster={cluster_id}: not found in clusters.yaml"]
    defaults = clusters_yaml.get("defaults") or {}

    runner_modules = _enabled_modules(cluster_cfg, "arc-runners")
    if not runner_modules:
        return []

    nodepool_modules = _enabled_modules(cluster_cfg, "nodepools")
    if not nodepool_modules:
        return [
            f"cluster={cluster_id}: has arc-runners modules ({runner_modules}) but no "
            f"nodepools* modules enabled — runner workflow pods cannot be scheduled. "
            f"Either enable a nodepools module or remove arc-runners from this cluster."
        ]

    region = _resolve_region(cluster_cfg, defaults)

    roots: list[Path] = [upstream_dir]
    if consumer_root is not None and consumer_root.resolve() != upstream_dir.resolve():
        roots.append(consumer_root)

    excluded_instance_types = _collect_excluded_instance_types(roots, nodepool_modules, region)
    available, errors = _collect_available_fleets(roots, nodepool_modules, region)

    for def_file, module in _collect_runner_defs(roots, runner_modules):
        err = _validate_one_runner(cluster_id, def_file, module, available, excluded_instance_types)
        if err:
            errors.append(err)

    return errors
