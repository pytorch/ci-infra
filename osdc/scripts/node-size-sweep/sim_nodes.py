"""Data model + allocatable/instance-selection helpers for the sweep simulator.

Loads fleet instance lists from `modules/nodepools*/defs/*.yaml`, computes per
(fleet, instance_type) allocatable capacity net of kubelet + DaemonSet overhead,
and picks the highest-weight instance in a fleet that fits a pod request.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts" / "python"))

from analyze_node_utilization import compute_allocatable, compute_daemonset_overhead  # noqa: E402
from daemonset_overhead import DaemonSetOverhead, discover_daemonsets, hf_cache_gpu_topup_mib  # noqa: E402
from fleet_naming import derive_fleet_name  # noqa: E402
from instance_specs import INSTANCE_SPECS  # noqa: E402

NODEPOOL_DEFS_DIRS = [
    REPO_ROOT / "modules" / "nodepools" / "defs",
    REPO_ROOT / "modules" / "nodepools-h100" / "defs",
    REPO_ROOT / "modules" / "nodepools-b200" / "defs",
]

FLEET_SCOPED_DAEMONSETS: dict[str, set[str]] = {
    "runner-hooks-warmer": {"c7i-runner"},
}


def _daemonsets_for_fleet(daemonsets: list[DaemonSetOverhead], fleet_name: str) -> list[DaemonSetOverhead]:
    """Filter DaemonSets whose nodeSelector restricts them to specific fleets.

    discover_daemonsets doesn't understand nodeSelector, so fleet-scoped DSes
    like runner-hooks-warmer (node-fleet=c7i-runner) would otherwise be counted
    on every fleet's allocatable calculation.
    """
    return [ds for ds in daemonsets if fleet_name in FLEET_SCOPED_DAEMONSETS.get(ds.name, {fleet_name})]


@dataclass(slots=True)
class Job:
    label: str
    pool: str
    cpu_m: int
    mem_mi: int
    gpu: int
    start_bucket: int
    end_bucket: int


@dataclass(slots=True)
class Placeholder:
    cpu_m: int
    mem_mi: int
    gpu: int
    created_bucket: int


@dataclass(slots=True)
class Node:
    pool: str
    instance_type: str
    cpu_allocatable_m: int
    mem_allocatable_mi: int
    gpu_allocatable: int
    cpu_used_m: int = 0
    mem_used_mi: int = 0
    gpu_used: int = 0
    live_jobs: int = 0
    placeholders: list[Placeholder] = field(default_factory=list)
    empty_since_bucket: int | None = None
    warming_until_bucket: int | None = None
    daemonset_cpu_m: int = 0
    daemonset_mem_mi: int = 0
    daemonset_gpu: int = 0
    phantom_cpu_m: int = 0
    phantom_mem_mi: int = 0
    phantom_gpu: int = 0


@dataclass(frozen=True)
class Allocatable:
    cpu_m: int
    mem_mi: int
    gpu: int


@dataclass(frozen=True)
class FleetSpec:
    """A fleet's available instance types, ordered highest-weight first."""

    name: str
    is_gpu: bool
    instances: tuple[str, ...]


def _load_fleet_specs(defs_dirs: list[Path]) -> dict[str, FleetSpec]:
    """Read every nodepool def YAML and return {fleet_name: FleetSpec}.

    Ignores `exclude_regions` — the sim is region-agnostic.
    """
    fleets: dict[str, FleetSpec] = {}
    nodepool_files: list[Path] = []
    for defs_dir in defs_dirs:
        if not defs_dir.is_dir():
            continue
        for f in sorted(defs_dir.glob("*.yaml")):
            data = yaml.safe_load(f.read_text()) or {}
            fleet = data.get("fleet")
            if isinstance(fleet, dict):
                name = fleet.get("name")
                if not isinstance(name, str):
                    continue
                instances = fleet.get("instances") or []
                ordered = sorted(
                    (i for i in instances if isinstance(i, dict) and i.get("type")),
                    key=lambda i: -int(i.get("weight", 0)),
                )
                types = tuple(i["type"] for i in ordered)
                if not types:
                    continue
                fleets[name] = FleetSpec(name=name, is_gpu=bool(fleet.get("gpu", False)), instances=types)
                continue
            if isinstance(data.get("nodepool"), dict):
                nodepool_files.append(f)

    for f in nodepool_files:
        data = yaml.safe_load(f.read_text()) or {}
        nodepool = data.get("nodepool") or {}
        instance_type = nodepool.get("instance_type")
        if not isinstance(instance_type, str):
            continue
        # nodepool: defs are single-instance pools keyed by taint, not by node-fleet
        # label; expose them as fleets under their instance-family name so the sim's
        # fleet lookup (which uses derive_fleet_name) can resolve them.
        derived = derive_fleet_name(instance_type)
        if derived in fleets:
            print(
                f"warning: nodepool def {f} would collide with existing fleet "
                f"{derived!r}; keeping explicit fleet: definition",
                file=sys.stderr,
            )
            continue
        fleets[derived] = FleetSpec(
            name=derived,
            is_gpu=bool(nodepool.get("gpu", False)),
            instances=(instance_type,),
        )
    return fleets


class ClusterModel:
    """Holds fleet specs + cached per-(fleet, instance) allocatable."""

    def __init__(
        self,
        defs_dirs: list[Path] | None = None,
        upstream_dir: Path = REPO_ROOT,
        fleets_override: dict[str, FleetSpec] | None = None,
        fleets_extra: dict[str, FleetSpec] | None = None,
    ):
        if fleets_override is not None:
            self.fleets = dict(fleets_override)
        else:
            if defs_dirs is None:
                defs_dirs = NODEPOOL_DEFS_DIRS
            self.fleets = _load_fleet_specs(defs_dirs)
        if fleets_extra:
            # Extras win on collision — caller is explicitly steering a fleet
            # name to a specific instance for a cluster-wide sim.
            self.fleets.update(fleets_extra)
        self.daemonsets = discover_daemonsets(upstream_dir)
        self._alloc_cache: dict[tuple[str, str], Allocatable] = {}
        self._ds_cache: dict[tuple[str, str], tuple[int, int, int]] = {}

    def allocatable(self, fleet: str, instance_type: str) -> Allocatable:
        key = (fleet, instance_type)
        cached = self._alloc_cache.get(key)
        if cached is not None:
            return cached
        scoped_ds = _daemonsets_for_fleet(self.daemonsets, fleet)
        info = compute_allocatable(instance_type, scoped_ds)
        alloc = Allocatable(
            cpu_m=info["allocatable_cpu_m"],
            mem_mi=info["allocatable_mem_mi"],
            gpu=info["allocatable_gpu"],
        )
        self._alloc_cache[key] = alloc
        return alloc

    def daemonset_totals(self, fleet: str, instance_type: str) -> tuple[int, int, int]:
        key = (fleet, instance_type)
        cached = self._ds_cache.get(key)
        if cached is not None:
            return cached
        spec = INSTANCE_SPECS.get(instance_type)
        is_gpu = bool(spec and spec.get("gpu", 0) > 0)
        scoped_ds = _daemonsets_for_fleet(self.daemonsets, fleet)
        ds_cpu, ds_mem = compute_daemonset_overhead(scoped_ds, is_gpu=is_gpu)
        # match compute_allocatable: per-GPU-count hf-cache reserve
        ds_mem += hf_cache_gpu_topup_mib(spec.get("gpu", 0)) if spec else 0
        totals = (ds_cpu, ds_mem, 0)
        self._ds_cache[key] = totals
        return totals

    def pick_instance(self, fleet: str, cpu_m: int, mem_mi: int, gpu: int) -> str:
        """Highest-weight instance in `fleet` whose allocatable fits the pod."""
        fs = self.fleets.get(fleet)
        if fs is None:
            raise RuntimeError(f"unknown fleet {fleet!r} (not in modules/nodepools*/defs)")
        for inst in fs.instances:
            if inst not in INSTANCE_SPECS:
                continue
            alloc = self.allocatable(fleet, inst)
            if alloc.cpu_m >= cpu_m and alloc.mem_mi >= mem_mi and alloc.gpu >= gpu:
                return inst
        raise RuntimeError(
            f"no instance in fleet {fleet!r} fits pod (cpu_m={cpu_m}, mem_mi={mem_mi}, gpu={gpu}); tried {fs.instances}"
        )

    def make_node(self, pool: str, cpu_m: int, mem_mi: int, gpu: int) -> Node:
        inst = self.pick_instance(pool, cpu_m, mem_mi, gpu)
        alloc = self.allocatable(pool, inst)
        ds_cpu, ds_mem, ds_gpu = self.daemonset_totals(pool, inst)
        return Node(
            pool=pool,
            instance_type=inst,
            cpu_allocatable_m=alloc.cpu_m,
            mem_allocatable_mi=alloc.mem_mi,
            gpu_allocatable=alloc.gpu,
            daemonset_cpu_m=ds_cpu,
            daemonset_mem_mi=ds_mem,
            daemonset_gpu=ds_gpu,
        )


def fits(node: Node, cpu_m: int, mem_mi: int, gpu: int) -> bool:
    return (
        node.cpu_allocatable_m - node.cpu_used_m >= cpu_m
        and node.mem_allocatable_mi - node.mem_used_mi >= mem_mi
        and node.gpu_allocatable - node.gpu_used >= gpu
    )


def most_allocated_score(node: Node) -> float:
    """MostAllocated ranking: highest of (cpu, mem, gpu) usage ratios."""
    cpu_frac = node.cpu_used_m / node.cpu_allocatable_m if node.cpu_allocatable_m > 0 else 0.0
    mem_frac = node.mem_used_mi / node.mem_allocatable_mi if node.mem_allocatable_mi > 0 else 0.0
    gpu_frac = node.gpu_used / node.gpu_allocatable if node.gpu_allocatable > 0 else 0.0
    return max(cpu_frac, mem_frac, gpu_frac)
