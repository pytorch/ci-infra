#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6.0"]
# ///
"""Compute the desired AutoscalingRunnerSet maxRunners map for a cluster.

Reads runner def YAMLs directly (no compile step) and emits a JSON object
keyed by the AutoscalingRunnerSet metadata.name (after the chart's
runnerScaleSetName -> metadata.name transformation).

Used by `just resume-runners <cluster>` to restore per-RSS maxRunners after
a `just drain-runners` cutover.

Output (stdout): JSON object, sorted keys, two-space indent.
Diagnostics: stderr only.

Example:
    uv run runner_max_map.py arc-cbr-production
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import yaml

# Import the SAME prefix-resolution path generate_runners.py uses to produce
# the ARS resources. Whole-block resolution (cluster's arc-runners dict wins
# entirely if present, otherwise defaults' dict) differs from per-key
# resolution when a cluster overrides only some keys — and we MUST match
# generate_runners.py exactly or `resume-runners` will compute ARS names that
# don't exist on the cluster. Both files live in the same directory so the
# import is direct; pytest's testpaths puts this dir on sys.path too.
from generate_runners import load_excluded_instance_types, resolve_max_runners, resolve_value

# The actions-runner-controller chart substitutes nil maxRunners with
# math.MaxInt32 (controllers/actions.github.com/resourcebuilder.go:94-101 in
# the upstream fork). Mirror that here so the JSON we emit is the literal
# value the controller would have produced — required so kubectl patches
# round-trip identically when the def omits max_runners.
MAX_INT32 = 2**31 - 1  # 2147483647


def find_repo_root(start: Path) -> Path:
    """Walk upwards from `start` until a directory containing clusters.yaml is found.

    Mirrors generate_runners.py's resolution: prefer OSDC_ROOT env var when set,
    otherwise walk up. Caller passes the script directory; we walk up its
    parents looking for clusters.yaml.

    Raises FileNotFoundError if no clusters.yaml is found before reaching the
    filesystem root.
    """
    if "OSDC_ROOT" in os.environ:
        root = Path(os.environ["OSDC_ROOT"])
        if not (root / "clusters.yaml").exists():
            raise FileNotFoundError(f"OSDC_ROOT={root} does not contain clusters.yaml")
        return root

    current = start.resolve()
    for candidate in (current, *current.parents):
        if (candidate / "clusters.yaml").exists():
            return candidate
    raise FileNotFoundError(f"clusters.yaml not found walking up from {start}")


def load_clusters_yaml(repo_root: Path) -> dict:
    """Load and parse clusters.yaml from the repository root."""
    with open(repo_root / "clusters.yaml") as f:
        return yaml.safe_load(f) or {}


def get_cluster(clusters_yaml: dict, cluster_id: str) -> dict:
    """Return the per-cluster config, raising KeyError if cluster_id is unknown."""
    clusters = clusters_yaml.get("clusters", {})
    if cluster_id not in clusters:
        known = ", ".join(sorted(clusters.keys())) or "<none>"
        raise KeyError(f"Unknown cluster '{cluster_id}'. Known clusters: {known}")
    return clusters[cluster_id]


def resolve_dotted(cluster_cfg: dict, defaults: dict, dotpath: str):
    """Resolve a dot-separated path against cluster config with defaults fallback.

    Mirrors scripts/cluster-config.py and generate_runners.py semantics so the
    same configuration values are read consistently across helpers.
    """
    parts = dotpath.split(".")
    val = cluster_cfg
    dval = defaults
    for part in parts:
        val = val.get(part) if isinstance(val, dict) else None
        dval = dval.get(part) if isinstance(dval, dict) else None
    if val is not None:
        return val
    return dval


def resolve_runner_name_prefix(clusters_yaml: dict, cluster_id: str) -> str:
    """Return arc-runners.runner_name_prefix for the cluster (defaults fallback).

    Uses generate_runners.py's WHOLE-BLOCK resolution (not per-key dotted lookup):
    the cluster's `arc-runners` dict wins entirely if present, else defaults' dict.
    See generate_runners.py:332 — `resolve_value(cluster_cfg, defaults, "arc-runners")`.
    Per-key dotted resolution would diverge when a cluster overrides only some
    keys (e.g. github_config_url) and leaves runner_name_prefix unset, falling
    through to defaults — but the chart actually generates ARS resources from
    the whole-block path, so that fallback would compute names that don't exist
    on the cluster.
    """
    cluster_cfg = get_cluster(clusters_yaml, cluster_id)
    defaults = clusters_yaml.get("defaults", {})
    arc_runners_config = resolve_value(cluster_cfg, defaults, "arc-runners") or {}
    return arc_runners_config.get("runner_name_prefix", "") or ""


def enabled_arc_runner_modules(clusters_yaml: dict, cluster_id: str) -> list[str]:
    """Return enabled module names matching `arc-runners` or `arc-runners-*`.

    Matches the variant pattern used by the GPU shims (arc-runners-h100,
    arc-runners-b200, ...) without hardcoding the variant list — any future
    variant module that follows the `arc-runners-<suffix>` naming will be
    picked up automatically.
    """
    cluster_cfg = get_cluster(clusters_yaml, cluster_id)
    modules = cluster_cfg.get("modules", []) or []
    return [m for m in modules if m == "arc-runners" or m.startswith("arc-runners-")]


def compute_ars_name(prefix: str, runner_name: str) -> str:
    """Return the AutoscalingRunnerSet metadata.name the chart will produce.

    The chart's autoscalingrunnerset.yaml computes:
        name: {{ include "gha-runner-scale-set.scale-set-name" . | replace "_" "-" }}
    where scale-set-name resolves to .Values.runnerScaleSetName.

    The values file emits runnerScaleSetName as the literal "{prefix}{name}"
    (generate_runners.py:258 — RUNNER_NAME placeholder is the raw def name,
    NOT the {{RUNNER_NAME_NORMALIZED}} variant). The chart then strips
    underscores. Dots are NOT stripped by the chart, so do not strip them
    here either — round-tripping requires byte-identical names.
    """
    return (prefix + runner_name).replace("_", "-")


def parse_def_file(def_file: Path, cluster_id: str) -> tuple[str, str | None, int | None]:
    """Read a runner def YAML and return (runner_name, instance_type, max_runners).

    max_runners is None when the def omits it (caller substitutes MAX_INT32).
    Delegates to `resolve_max_runners` for the int-vs-mapping logic.
    instance_type is returned so the caller can apply the nodepool
    exclude_regions guard. Raises ValueError if `runner.name` is missing or
    `max_runners` is invalid.
    """
    with open(def_file) as f:
        data = yaml.safe_load(f) or {}

    runner = data.get("runner") or {}
    name = runner.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError(f"Invalid def file {def_file}: missing required 'runner.name'")

    instance_type = runner.get("instance_type")
    if instance_type is not None and not isinstance(instance_type, str):
        raise ValueError(f"Invalid def file {def_file}: instance_type must be a string, got {instance_type!r}")

    max_runners = resolve_max_runners(runner.get("max_runners"), def_file, cluster_id)
    return name, instance_type, max_runners


def iter_def_files(repo_root: Path, module: str) -> list[Path]:
    """Return sorted list of *.yaml files under modules/<module>/defs/.

    Returns an empty list (not an error) when the module directory or its
    defs subdirectory does not exist, so a misconfigured cluster does not
    abort the whole map. Caller may log a stderr warning.
    """
    defs_dir = repo_root / "modules" / module / "defs"
    if not defs_dir.is_dir():
        return []
    return sorted(defs_dir.glob("*.yaml"))


def build_max_runners_map(repo_root: Path, clusters_yaml: dict, cluster_id: str) -> dict[str, int]:
    """Build {ars_name: max_runners} for every def in every enabled arc-runners* module.

    Pure function (modulo file reads). Raises if cluster is unknown or any def
    is malformed. Logs to stderr when a module has no defs directory.

    Runners whose instance_type is in a nodepool/fleet def that excludes the
    cluster's region render with max_runners=0 — mirrors the same guard in
    generate_runners.py so `just resume-runners` does not patch excluded
    scale sets back to MAX_INT32 after a drain.

    When the cluster has pause_runners=true, every ARS renders with
    max_runners=0 — mirrors generate_runners.py so `just resume-runners` does
    not silently undo a pause by patching scale sets back to their def values.
    """
    prefix = resolve_runner_name_prefix(clusters_yaml, cluster_id)
    modules = enabled_arc_runner_modules(clusters_yaml, cluster_id)

    cluster_cfg = get_cluster(clusters_yaml, cluster_id)
    region = cluster_cfg.get("region", "")
    nodepools_defs_dir = repo_root / "modules" / "nodepools" / "defs"
    excluded_instance_types = load_excluded_instance_types(nodepools_defs_dir, region)
    paused = bool(cluster_cfg.get("pause_runners"))

    result: dict[str, int] = {}
    for module in modules:
        def_files = iter_def_files(repo_root, module)
        if not def_files:
            print(f"warning: module '{module}' has no def files at modules/{module}/defs/", file=sys.stderr)
            continue
        for def_file in def_files:
            name, instance_type, max_runners = parse_def_file(def_file, cluster_id)
            ars_name = compute_ars_name(prefix, name)
            if paused or instance_type in excluded_instance_types:
                result[ars_name] = 0
            else:
                result[ars_name] = max_runners if max_runners is not None else MAX_INT32
    return result


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint. Prints JSON map to stdout, diagnostics to stderr."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1:
        print("Usage: runner_max_map.py <cluster-id>", file=sys.stderr)
        print("Example: runner_max_map.py arc-cbr-production", file=sys.stderr)
        return 2

    cluster_id = argv[0]

    try:
        repo_root = find_repo_root(Path(__file__).parent)
        clusters_yaml = load_clusters_yaml(repo_root)
        result = build_max_runners_map(repo_root, clusters_yaml, cluster_id)
    except (FileNotFoundError, KeyError, ValueError, yaml.YAMLError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    print(json.dumps(result, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
