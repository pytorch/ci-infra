"""Resolve cluster topology — fleets, nodepools, and runners — for a given cluster.

Answers "for cluster X, which fleets/nodepools are deployed, and which runners are
schedulable here?" Consumed by analyze_node_utilization.py and simulate_cluster.py.

Inputs (disk only, no live cluster access):
  1. <consumer_root>/clusters.yaml — cluster -> modules + per-cluster overrides.
  2. <upstream_root>/modules/nodepools[-b200]/defs/*.yaml — fleet/fleets/legacy
     nodepool schemas. Honors ``exclude_regions:`` like generate_nodepools.py.
  3. <upstream_root>/modules/arc-runners[-b200]/generated/*.yaml — we READ the
     generator's output rather than re-rendering templates.

IMPORTANT: caller MUST run ``generate-runners`` first if generated/ may be stale.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import yaml

if TYPE_CHECKING:
    from pathlib import Path


@dataclass
class NodePoolEntry:
    """Deployed Karpenter NodePool. runner_class='release' when from a release sub-fleet."""

    name: str
    fleet: str
    instance_type: str
    arch: str  # "amd64" | "arm64"
    gpu: bool
    runner_class: str | None


@dataclass
class RunnerEntry:
    """Runner scale-set from a generated manifest. schedulable_reason explains False."""

    name: str
    scale_set_name: str
    instance_type: str
    workflow_fleet: str
    runner_class: str | None
    runner_pod_cpu_m: int
    runner_pod_mem_mi: int
    workflow_pod_cpu_m: int
    workflow_pod_mem_mi: int
    workflow_pod_gpu: int
    schedulable: bool
    schedulable_reason: str | None


@dataclass
class ClusterTopology:
    """Full topology — runner_pool_fleet is 'c7i-runner' when present, else None (warns)."""

    cluster_id: str
    region: str
    modules: list[str]
    nodepools: list[NodePoolEntry]
    runner_pool_fleet: str | None
    workflow_pool_fleets: set[str] = field(default_factory=set)
    runners: list[RunnerEntry] = field(default_factory=list)


def is_excluded_for_region(def_block: dict[str, Any], region: str | None) -> bool:
    """Mirror generate_nodepools._is_excluded_for_region. No-op when region/key absent."""
    if not region:
        return False
    return region in (def_block.get("exclude_regions") or [])


def derive_fleet_name(instance_type: str) -> str:
    """Instance family prefix: c7i.48xlarge -> c7i, p6-b200.48xlarge -> p6-b200."""
    return instance_type.split(".")[0]


def fleet_nodepool_name(fleet_name: str, instance_type: str, name_suffix: str = "") -> str:
    """Mirror generate_nodepools._fleet_nodepool_name. <instance>-<size>; <fleet>-<size> when names diverge."""
    instance_family = instance_type.split(".")[0]
    if fleet_name == instance_family:
        name = instance_type.replace(".", "-")
    else:
        instance_size = instance_type.split(".", 1)[1].replace(".", "-")
        name = f"{fleet_name}-{instance_size}"
    return f"{name}{name_suffix}" if name_suffix else name


def parse_cpu(value: str | int | float) -> int:
    """K8s CPU string -> milliCPU. ``"6"`` -> 6000, ``"750m"`` -> 750, ``"6.5"`` -> 6500."""
    if isinstance(value, int | float):
        return int(value * 1000)
    s = str(value).strip()
    if s.endswith("m"):
        return int(float(s[:-1]))
    return int(float(s) * 1000)


def parse_memory(value: str | int) -> int:
    """K8s memory string -> MiB. Plain numbers = bytes (matches analyze_node_utilization)."""
    s = str(value).strip()
    if s.endswith("Gi"):
        return int(float(s[:-2]) * 1024)
    if s.endswith("Mi"):
        return int(float(s[:-2]))
    if s.endswith("Ki"):
        return int(float(s[:-2]) / 1024)
    return int(int(s) / (1024 * 1024))


def _load_cluster_entry(cluster_id: str, consumer_root: Path) -> dict[str, Any]:
    path = consumer_root / "clusters.yaml"
    if not path.exists():
        raise FileNotFoundError(f"clusters.yaml not found at {path}")
    with open(path) as f:
        data = yaml.safe_load(f)
    if not data or "clusters" not in data:
        raise ValueError(f"{path}: missing 'clusters' top-level key")
    clusters = data.get("clusters") or {}
    if cluster_id not in clusters:
        known = ", ".join(sorted(clusters.keys()))
        raise KeyError(f"Cluster '{cluster_id}' not in clusters.yaml (known: {known})")
    entry = clusters[cluster_id]
    if not isinstance(entry, dict):
        raise ValueError(f"clusters.yaml: cluster '{cluster_id}' is not a mapping")
    return entry


def _build_nodepool_entry(
    fleet_data: dict[str, Any],
    inst: dict[str, Any],
    *,
    name_suffix: str = "",
    runner_class: str | None = None,
) -> NodePoolEntry:
    instance_type = inst["type"]
    fleet_name = fleet_data["name"]
    return NodePoolEntry(
        name=fleet_nodepool_name(fleet_name, instance_type, name_suffix),
        fleet=fleet_name,
        instance_type=instance_type,
        arch=fleet_data.get("arch", "amd64"),
        gpu=bool(fleet_data.get("gpu", False)),
        runner_class=runner_class,
    )


def _entries_from_fleet(fleet_data: dict[str, Any], region: str | None) -> list[NodePoolEntry]:
    if is_excluded_for_region(fleet_data, region):
        return []
    entries: list[NodePoolEntry] = []
    for inst in fleet_data.get("instances") or []:
        entries.append(_build_nodepool_entry(fleet_data, inst))
    for inst in fleet_data.get("release") or []:
        entries.append(_build_nodepool_entry(fleet_data, inst, name_suffix="-release", runner_class="release"))
    return entries


def _entries_from_legacy_pool(pool_data: dict[str, Any], region: str | None) -> list[NodePoolEntry]:
    """Legacy ``nodepool:`` block (b200 currently). Fleet auto-derived from instance_type."""
    if is_excluded_for_region(pool_data, region):
        return []
    instance_type = pool_data["instance_type"]
    return [
        NodePoolEntry(
            name=pool_data["name"],
            fleet=derive_fleet_name(instance_type),
            instance_type=instance_type,
            arch=pool_data.get("arch", "amd64"),
            gpu=bool(pool_data.get("gpu", False)),
            runner_class=None,
        )
    ]


def _walk_nodepool_defs(defs_dir: Path, region: str | None) -> list[NodePoolEntry]:
    entries: list[NodePoolEntry] = []
    for def_file in sorted(defs_dir.glob("*.yaml")):
        with open(def_file) as f:
            data = yaml.safe_load(f) or {}
        if "fleet" in data:
            entries.extend(_entries_from_fleet(data["fleet"], region))
        elif "fleets" in data:
            for fleet_data in data["fleets"]:
                entries.extend(_entries_from_fleet(fleet_data, region))
        elif "nodepool" in data:
            entries.extend(_entries_from_legacy_pool(data["nodepool"], region))
        else:
            print(f"[cluster_topology] WARN: {def_file.name}: missing 'fleet'/'fleets'/'nodepool' key")
    return entries


def load_nodepools(modules: list[str], region: str | None, *, upstream_root: Path) -> list[NodePoolEntry]:
    """Walk nodepools/ (and nodepools-b200/ if enabled) defs, honoring both schemas + region filter."""
    entries: list[NodePoolEntry] = []
    for module_name in ("nodepools", "nodepools-b200"):
        if module_name not in modules:
            continue
        defs_dir = upstream_root / "modules" / module_name / "defs"
        if defs_dir.exists():
            entries.extend(_walk_nodepool_defs(defs_dir, region))
        else:
            print(f"[cluster_topology] WARN: {module_name} defs dir not found: {defs_dir}")
    return entries


def _container_requests(container: dict[str, Any]) -> dict[str, str]:
    return ((container or {}).get("resources") or {}).get("requests") or {}


def _extract_workflow_fleet(pod_spec: dict[str, Any]) -> str | None:
    """Search preferred nodeAffinity matchExpressions for the node-fleet value (no hardcoded indices)."""
    node_aff = ((pod_spec or {}).get("affinity") or {}).get("nodeAffinity") or {}
    for pref in node_aff.get("preferredDuringSchedulingIgnoredDuringExecution") or []:
        for expr in ((pref or {}).get("preference") or {}).get("matchExpressions") or []:
            if expr.get("key") == "node-fleet":
                values = expr.get("values") or []
                if values:
                    return str(values[0])
    return None


def _extract_runner_class(pod_spec: dict[str, Any]) -> str | None:
    """Search required nodeAffinity for osdc.io/runner-class In value. None when DoesNotExist."""
    node_aff = ((pod_spec or {}).get("affinity") or {}).get("nodeAffinity") or {}
    required = node_aff.get("requiredDuringSchedulingIgnoredDuringExecution") or {}
    for term in required.get("nodeSelectorTerms") or []:
        for expr in term.get("matchExpressions") or []:
            if expr.get("key") == "osdc.io/runner-class" and expr.get("operator") == "In":
                values = expr.get("values") or []
                if values:
                    return str(values[0])
    return None


def _resolve_instance_type_from_def(generated_path: Path, runner_name: str) -> str | None:
    """Look up the runner's instance_type from its source def file (sibling to generated/)."""
    def_file = generated_path.parent.parent / "defs" / f"{runner_name}.yaml"
    if not def_file.exists():
        return None
    with open(def_file) as f:
        data = yaml.safe_load(f) or {}
    inst = (data.get("runner") or {}).get("instance_type")
    return str(inst) if inst else None


def _warn_skip(path: Path, reason: str) -> None:
    print(f"[cluster_topology] WARN: {path.name}: {reason} — skipping")


def _parse_one_generated(path: Path) -> RunnerEntry | None:
    """Parse a single generated runner manifest into a RunnerEntry (no schedulability set)."""
    with open(path) as f:
        docs = list(yaml.safe_load_all(f))
    if len(docs) < 2:
        _warn_skip(path, f"expected 2 YAML docs, got {len(docs)}")
        return None

    helm_values, configmap = docs[0] or {}, docs[1] or {}
    if not isinstance(helm_values, dict) or not isinstance(configmap, dict):
        _warn_skip(path, "malformed docs")
        return None

    # Doc 1: Helm values, runner pod under template.spec.
    containers = ((helm_values.get("template") or {}).get("spec") or {}).get("containers") or []
    if not containers:
        _warn_skip(path, "no runner containers")
        return None
    runner_requests = _container_requests(containers[0])

    # Doc 2: ConfigMap with workflow pod YAML string at data['job-pod.yaml'].
    if configmap.get("kind") != "ConfigMap":
        _warn_skip(path, "doc 2 is not a ConfigMap")
        return None
    job_pod_yaml = (configmap.get("data") or {}).get("job-pod.yaml")
    if not job_pod_yaml:
        _warn_skip(path, "ConfigMap missing data['job-pod.yaml']")
        return None
    workflow_spec = (yaml.safe_load(job_pod_yaml) or {}).get("spec") or {}
    workflow_containers = workflow_spec.get("containers") or []
    if not workflow_containers:
        _warn_skip(path, "no workflow containers")
        return None
    workflow_requests = _container_requests(workflow_containers[0])

    cm_name = (configmap.get("metadata") or {}).get("name") or ""
    runner_name = cm_name.removeprefix("arc-runner-hook-") if cm_name else path.stem

    workflow_fleet = _extract_workflow_fleet(workflow_spec)
    if not workflow_fleet:
        _warn_skip(path, "could not determine workflow node-fleet")
        return None

    # instance_type isn't in the generated file directly — pull from the source def.
    instance_type = _resolve_instance_type_from_def(path, runner_name) or f"{workflow_fleet}.unknown"

    return RunnerEntry(
        name=runner_name,
        scale_set_name=str(helm_values.get("runnerScaleSetName") or runner_name),
        instance_type=instance_type,
        workflow_fleet=workflow_fleet,
        runner_class=_extract_runner_class(workflow_spec),
        runner_pod_cpu_m=parse_cpu(runner_requests.get("cpu", "0")),
        runner_pod_mem_mi=parse_memory(runner_requests.get("memory", "0")),
        workflow_pod_cpu_m=parse_cpu(workflow_requests.get("cpu", "0")),
        workflow_pod_mem_mi=parse_memory(workflow_requests.get("memory", "0")),
        workflow_pod_gpu=int(workflow_requests.get("nvidia.com/gpu", 0) or 0),
        schedulable=True,
        schedulable_reason=None,
    )


def load_runners(modules: list[str], *, upstream_root: Path) -> list[RunnerEntry]:
    """Read generated runner manifests for the modules enabled on this cluster."""
    runners: list[RunnerEntry] = []
    for module_name in ("arc-runners", "arc-runners-b200"):
        if module_name not in modules:
            continue
        gen_dir = upstream_root / "modules" / module_name / "generated"
        if not gen_dir.exists():
            print(f"[cluster_topology] WARN: generated runner dir not found: {gen_dir}")
            continue
        for path in sorted(gen_dir.glob("*.yaml")):
            entry = _parse_one_generated(path)
            if entry is not None:
                runners.append(entry)
    return runners


def _release_pool_exists(nodepools: list[NodePoolEntry], fleet: str) -> bool:
    return any(p.fleet == fleet and p.runner_class == "release" for p in nodepools)


def _mark_schedulability(
    runners: list[RunnerEntry],
    nodepools: list[NodePoolEntry],
    workflow_pool_fleets: set[str],
) -> None:
    """Set schedulable + schedulable_reason on each runner in-place."""
    available = sorted(workflow_pool_fleets) or "none"
    for runner in runners:
        if runner.workflow_fleet not in workflow_pool_fleets:
            runner.schedulable = False
            runner.schedulable_reason = (
                f"workflow_fleet '{runner.workflow_fleet}' not in deployed workflow pools (available: {available})"
            )
            continue
        if runner.runner_class == "release" and not _release_pool_exists(nodepools, runner.workflow_fleet):
            runner.schedulable = False
            runner.schedulable_reason = (
                f"runner_class=release but no '{runner.workflow_fleet}-*-release' nodepool deployed"
            )
            continue
        runner.schedulable = True
        runner.schedulable_reason = None


def resolve_cluster(
    cluster_id: str,
    *,
    upstream_root: Path,
    consumer_root: Path,
) -> ClusterTopology:
    """Resolve full topology. Caller MUST run ``generate-runners`` first if generated/ may be stale."""
    cluster_entry = _load_cluster_entry(cluster_id, consumer_root)
    region = cluster_entry.get("region")
    modules = list(cluster_entry.get("modules") or [])

    nodepools = load_nodepools(modules, region, upstream_root=upstream_root)
    runners = load_runners(modules, upstream_root=upstream_root)

    # c7i-runner is the dedicated runner-pod fleet (PROACTIVE_CAPACITY.md). When
    # absent, runners share workflow fleets — warn but don't fail.
    fleets_present = {p.fleet for p in nodepools}
    runner_pool_fleet = "c7i-runner" if "c7i-runner" in fleets_present else None
    if runner_pool_fleet is None:
        print(
            f"[cluster_topology] WARN: cluster '{cluster_id}': no 'c7i-runner' fleet deployed; "
            "runner pods will share workflow fleets"
        )
    workflow_pool_fleets = {f for f in fleets_present if f != runner_pool_fleet}

    _mark_schedulability(runners, nodepools, workflow_pool_fleets)

    return ClusterTopology(
        cluster_id=cluster_id,
        region=str(region) if region else "",
        modules=modules,
        nodepools=nodepools,
        runner_pool_fleet=runner_pool_fleet,
        workflow_pool_fleets=workflow_pool_fleets,
        runners=runners,
    )
