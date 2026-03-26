"""Data models and resource parsing for the Node Compactor."""

import logging
import math
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime

from lightkube.resources.core_v1 import Pod

log = logging.getLogger("compactor")

# ============================================================================
# Annotation keys
# ============================================================================

ANNOTATION_CAPACITY_RESERVED = "node-compactor.osdc.io/capacity-reserved"
ANNOTATION_DO_NOT_DISRUPT = "karpenter.sh/do-not-disrupt"

# ============================================================================
# Configuration
# ============================================================================

DEFAULTS = {
    "COMPACTOR_INTERVAL": "20",
    "COMPACTOR_MAX_UPTIME_HOURS": "48",
    "COMPACTOR_NODEPOOL_LABEL": "osdc.io/node-compactor",
    "COMPACTOR_TAINT_KEY": "node-compactor.osdc.io/consolidating",
    "COMPACTOR_MIN_NODES": "1",
    "COMPACTOR_DRY_RUN": "false",
    "COMPACTOR_TAINT_COOLDOWN": "300",
    "COMPACTOR_MIN_NODE_AGE": "900",
    "COMPACTOR_FLEET_COOLDOWN": "900",
    "COMPACTOR_TAINT_RATE": "0.3",
    "COMPACTOR_SPARE_CAPACITY_NODES": "3",
    "COMPACTOR_SPARE_CAPACITY_RATIO": "0.15",
    "COMPACTOR_SPARE_CAPACITY_THRESHOLD": "0.4",
    "COMPACTOR_CAPACITY_RESERVATION_NODES": "0",
}


@dataclass(frozen=True)
class Config:
    interval: int
    max_uptime_hours: int
    nodepool_label: str
    taint_key: str
    min_nodes: int
    dry_run: bool
    taint_cooldown: int
    min_node_age: int
    fleet_cooldown: int
    taint_rate: float
    spare_capacity_nodes: int
    spare_capacity_ratio: float
    spare_capacity_threshold: float
    capacity_reservation_nodes: int

    @classmethod
    def from_env(cls) -> "Config":
        def env(key: str) -> str:
            return os.environ.get(key, DEFAULTS[key])

        return cls(
            interval=int(env("COMPACTOR_INTERVAL")),
            max_uptime_hours=int(env("COMPACTOR_MAX_UPTIME_HOURS")),
            nodepool_label=env("COMPACTOR_NODEPOOL_LABEL"),
            taint_key=env("COMPACTOR_TAINT_KEY"),
            min_nodes=int(env("COMPACTOR_MIN_NODES")),
            dry_run=env("COMPACTOR_DRY_RUN").lower() in ("true", "1", "yes"),
            taint_cooldown=int(env("COMPACTOR_TAINT_COOLDOWN")),
            min_node_age=int(env("COMPACTOR_MIN_NODE_AGE")),
            fleet_cooldown=int(env("COMPACTOR_FLEET_COOLDOWN")),
            taint_rate=float(env("COMPACTOR_TAINT_RATE")),
            spare_capacity_nodes=int(env("COMPACTOR_SPARE_CAPACITY_NODES")),
            spare_capacity_ratio=float(env("COMPACTOR_SPARE_CAPACITY_RATIO")),
            spare_capacity_threshold=float(env("COMPACTOR_SPARE_CAPACITY_THRESHOLD")),
            capacity_reservation_nodes=int(env("COMPACTOR_CAPACITY_RESERVATION_NODES")),
        )


# ============================================================================
# Data models
# ============================================================================


@dataclass
class PodInfo:
    """Lightweight pod representation for bin-packing."""

    name: str
    namespace: str
    cpu_request: float  # in cores
    memory_request: int  # in bytes
    node_name: str
    is_daemonset: bool
    start_time: datetime | None = None
    is_phantom: bool = False


@dataclass
class NodeState:
    """Computed state of a managed node."""

    name: str
    nodepool: str
    allocatable_cpu: float  # cores
    allocatable_memory: int  # bytes
    creation_time: datetime
    pods: list[PodInfo] = field(default_factory=list)
    is_tainted: bool = False
    is_reserved: bool = False  # has capacity-reservation annotation
    node_taints: list = field(default_factory=list)  # raw taint objects from API
    labels: dict = field(default_factory=dict)  # node metadata labels
    annotations: dict = field(default_factory=dict)  # node metadata annotations

    @property
    def workload_pods(self) -> list[PodInfo]:
        return [p for p in self.pods if not p.is_daemonset]

    @property
    def workload_pod_count(self) -> int:
        return len(self.workload_pods)

    @property
    def daemonset_cpu(self) -> float:
        """Total CPU requests from DaemonSet pods."""
        return sum(p.cpu_request for p in self.pods if p.is_daemonset)

    @property
    def daemonset_memory(self) -> int:
        """Total memory requests from DaemonSet pods."""
        return sum(p.memory_request for p in self.pods if p.is_daemonset)

    @property
    def cpu_used(self) -> float:
        """CPU used by workload (non-DaemonSet) pods."""
        return sum(p.cpu_request for p in self.workload_pods)

    @property
    def memory_used(self) -> int:
        """Memory used by workload (non-DaemonSet) pods."""
        return sum(p.memory_request for p in self.workload_pods)

    @property
    def total_cpu_used(self) -> float:
        """Total CPU used by ALL pods (workload + DaemonSet)."""
        return sum(p.cpu_request for p in self.pods)

    @property
    def total_memory_used(self) -> int:
        """Total memory used by ALL pods (workload + DaemonSet)."""
        return sum(p.memory_request for p in self.pods)

    @property
    def cpu_utilization(self) -> float:
        if self.allocatable_cpu <= 0:
            return 0.0
        return self.cpu_used / self.allocatable_cpu

    @property
    def memory_utilization(self) -> float:
        if self.allocatable_memory <= 0:
            return 0.0
        return self.memory_used / self.allocatable_memory

    @property
    def utilization(self) -> float:
        """Max of CPU and memory utilization (conservative estimate)."""
        return max(self.cpu_utilization, self.memory_utilization)

    @property
    def uptime_seconds(self) -> float:
        return (datetime.now(UTC) - self.creation_time).total_seconds()

    @property
    def uptime_hours(self) -> float:
        return self.uptime_seconds / 3600

    @property
    def youngest_pod_age_seconds(self) -> float:
        """Age of the youngest workload pod in seconds.

        Nodes whose youngest pod is oldest are closer to draining naturally.
        Returns inf if no workload pods (empty nodes drain immediately).
        """
        now = datetime.now(UTC)
        ages = []
        for p in self.workload_pods:
            if p.start_time:
                ages.append((now - p.start_time).total_seconds())
        if not ages:
            return math.inf
        return min(ages)


# ============================================================================
# Resource parsing
# ============================================================================


def parse_cpu(value: str | int | float) -> float:
    """Parse Kubernetes CPU quantity to cores (float)."""
    try:
        s = str(value)
        if s.endswith("m"):
            return float(s[:-1]) / 1000
        if s.endswith("n"):
            return float(s[:-1]) / 1_000_000_000
        return float(s)
    except (ValueError, TypeError):
        log.warning("Failed to parse CPU value: %r, returning 0", value)
        return 0.0


def parse_memory(value: str | int | float) -> int:
    """Parse Kubernetes memory quantity to bytes (int)."""
    try:
        s = str(value)
        suffixes = {
            "Ki": 1024,
            "Mi": 1024**2,
            "Gi": 1024**3,
            "Ti": 1024**4,
            "K": 1000,
            "M": 1000**2,
            "G": 1000**3,
            "T": 1000**4,
        }
        for suffix, multiplier in suffixes.items():
            if s.endswith(suffix):
                return int(float(s[: -len(suffix)]) * multiplier)
        return int(s)
    except (ValueError, TypeError):
        log.warning("Failed to parse memory value: %r, returning 0", value)
        return 0


def is_daemonset_pod(pod: Pod) -> bool:
    """Check if a pod is owned by a DaemonSet."""
    if not pod.metadata or not pod.metadata.ownerReferences:
        return False
    return any(ref.kind == "DaemonSet" for ref in pod.metadata.ownerReferences)


def pod_cpu_request(pod: Pod) -> float:
    """Sum CPU requests across all containers in a pod."""
    total = 0.0
    if pod.spec and pod.spec.containers:
        for c in pod.spec.containers:
            if c.resources and c.resources.requests and "cpu" in c.resources.requests:
                total += parse_cpu(c.resources.requests["cpu"])
    return total


def pod_memory_request(pod: Pod) -> int:
    """Sum memory requests across all containers in a pod."""
    total = 0
    if pod.spec and pod.spec.containers:
        for c in pod.spec.containers:
            if c.resources and c.resources.requests and "memory" in c.resources.requests:
                total += parse_memory(c.resources.requests["memory"])
    return total
