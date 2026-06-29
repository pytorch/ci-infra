"""Unit tests for the pending module's pending_pods_for_group filter."""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

from models import (
    PENDING_POD_MAX_AGE_SECONDS,
    Config,
    NodeState,
    PodInfo,
)
from pending import pending_pods_for_group

NOW = datetime.now(UTC)
GiB = 1024**3


def make_config(**overrides) -> Config:
    defaults = {
        "interval": 20,
        "max_uptime_hours": 48,
        "nodepool_label": "osdc.io/node-compactor",
        "taint_key": "node-compactor.osdc.io/consolidating",
        "min_nodes": 1,
        "dry_run": False,
        "taint_cooldown": 300,
        "min_node_age": 900,
        "fleet_cooldown": 900,
        "taint_rate": 1.0,
        "spare_capacity_nodes": 0,
        "spare_capacity_ratio": 0.0,
        "spare_capacity_threshold": 0.4,
        "capacity_reservation_nodes": 0,
    }
    defaults.update(overrides)
    return Config(**defaults)


def make_node(
    name: str,
    nodepool: str = "default",
    cpu: float = 16.0,
    mem: int = 64 * GiB,
    gpu: int = 0,
    is_tainted: bool = False,
    creation_time: datetime | None = None,
) -> NodeState:
    return NodeState(
        name=name,
        nodepool=nodepool,
        allocatable_cpu=cpu,
        allocatable_memory=mem,
        allocatable_gpu=gpu,
        creation_time=creation_time or NOW - timedelta(hours=1),
        is_tainted=is_tainted,
    )


def make_pending_pod_mock(
    cpu: str = "1",
    memory: str = "1Gi",
    gpu: int | None = None,
    age_seconds: float = 60.0,
    name: str = "pending-pod",
    namespace: str = "default",
    tolerations=None,
    node_selector=None,
):
    """Build a MagicMock lightkube Pod for pending-pod tests."""
    pod = MagicMock()
    pod.metadata.name = name
    pod.metadata.namespace = namespace
    pod.metadata.creationTimestamp = datetime.now(UTC) - timedelta(seconds=age_seconds)
    pod.spec.tolerations = tolerations
    pod.spec.nodeSelector = node_selector
    pod.spec.affinity = None
    container = MagicMock()
    requests = {"cpu": cpu, "memory": memory}
    if gpu is not None:
        requests["nvidia.com/gpu"] = gpu
    container.resources.requests = requests
    pod.spec.containers = [container]
    return pod


class TestPendingPodsForGroup:
    """Direct tests for pending_pods_for_group (no compute_taints integration)."""

    def test_pending_pod_too_big_is_excluded(self):
        """96-CPU pending pod on 16-CPU nodes is excluded by resource sanity."""
        cfg = make_config(min_nodes=1, taint_rate=1.0)
        nodes = {f"n{i}": make_node(f"n{i}", cpu=16.0) for i in range(3)}

        pp_huge = make_pending_pod_mock(cpu="96", memory="1Gi")

        filtered = pending_pods_for_group([pp_huge], list(nodes.values()), cfg.taint_key)
        assert filtered == []

    def test_pending_pod_older_than_max_age_is_excluded(self):
        """Pod with age > PENDING_POD_MAX_AGE_SECONDS is filtered out."""
        cfg = make_config(min_nodes=1, taint_rate=1.0)
        nodes = {f"n{i}": make_node(f"n{i}") for i in range(3)}

        pp_stuck = make_pending_pod_mock(cpu="16", age_seconds=PENDING_POD_MAX_AGE_SECONDS + 100)

        filtered = pending_pods_for_group([pp_stuck], list(nodes.values()), cfg.taint_key)
        assert filtered == []

    def test_pending_pod_just_created_is_included(self):
        """Pod with age 0 (just created) is included — lower bound is 0."""
        cfg = make_config(min_nodes=1, taint_rate=1.0)
        nodes = {f"n{i}": make_node(f"n{i}") for i in range(3)}

        pp_new = make_pending_pod_mock(cpu="16", age_seconds=0)

        filtered = pending_pods_for_group([pp_new], list(nodes.values()), cfg.taint_key)
        assert len(filtered) == 1

    def test_pending_pod_with_future_timestamp_is_excluded(self):
        """Pod with creationTimestamp in the future (clock skew) is filtered out defensively."""
        cfg = make_config(min_nodes=1, taint_rate=1.0)
        nodes = {f"n{i}": make_node(f"n{i}") for i in range(3)}

        pp_future = make_pending_pod_mock(cpu="16", age_seconds=-30)

        filtered = pending_pods_for_group([pp_future], list(nodes.values()), cfg.taint_key)
        assert filtered == []

    def test_pending_pod_missing_creation_timestamp_skipped(self):
        """Pod without metadata.creationTimestamp is skipped (defensive)."""
        cfg = make_config(min_nodes=1, taint_rate=1.0)
        nodes = {f"n{i}": make_node(f"n{i}") for i in range(3)}

        pp = make_pending_pod_mock(cpu="16")
        pp.metadata.creationTimestamp = None

        filtered = pending_pods_for_group([pp], list(nodes.values()), cfg.taint_key)
        assert filtered == []

    def test_pending_pods_empty_inputs_return_empty(self):
        """Empty pending list or empty node list returns []."""
        nodes = [make_node("n1")]
        assert pending_pods_for_group([], nodes, "k") == []
        pp = make_pending_pod_mock(cpu="1")
        assert pending_pods_for_group([pp], [], "k") == []

    def test_pending_pod_compactor_taint_stripped_when_matching(self):
        """A pod with no tolerations still matches a node tainted only by compactor."""
        cfg = make_config(min_nodes=1, taint_rate=1.0)
        compactor_taint = MagicMock()
        compactor_taint.key = cfg.taint_key
        compactor_taint.value = "true"
        compactor_taint.effect = "NoSchedule"
        n = make_node("n1")
        n.node_taints = [compactor_taint]
        nodes_list = [n, make_node("n2"), make_node("n3")]

        pp = make_pending_pod_mock(cpu="1", memory="1Gi", tolerations=None)

        filtered = pending_pods_for_group([pp], nodes_list, cfg.taint_key)
        assert len(filtered) == 1

    def test_pending_pod_with_gpu_too_big_excluded(self):
        """Pending pod requesting more GPUs than any node has is excluded."""
        cfg = make_config(min_nodes=1, taint_rate=1.0)
        nodes_list = [make_node("n1"), make_node("n2")]

        pp = make_pending_pod_mock(cpu="1", memory="1Gi", gpu=2)

        filtered = pending_pods_for_group([pp], nodes_list, cfg.taint_key)
        assert filtered == []

    def test_pending_pod_metadata_none_skipped(self):
        """Pod whose metadata is None is skipped defensively."""
        cfg = make_config(min_nodes=1, taint_rate=1.0)
        nodes_list = [make_node("n1")]

        pp = MagicMock()
        pp.metadata = None

        filtered = pending_pods_for_group([pp], nodes_list, cfg.taint_key)
        assert filtered == []

    def test_pending_pod_equal_to_allocatable_excluded_when_daemonset_overhead(self):
        """A pod requesting exactly allocatable_cpu cannot fit once DaemonSets are accounted for."""
        cfg = make_config(min_nodes=1, taint_rate=1.0)
        ds_pod = PodInfo(
            name="ds",
            namespace="kube-system",
            cpu_request=0.5,
            memory_request=512 * 1024 * 1024,
            node_name="n1",
            is_daemonset=True,
            start_time=NOW,
        )
        node = make_node("n1", cpu=16.0)
        node.pods = [ds_pod]

        pp = make_pending_pod_mock(cpu="16", memory="1Gi")

        filtered = pending_pods_for_group([pp], [node], cfg.taint_key)
        assert filtered == []

    def test_pending_pod_at_allocatable_minus_daemonset_admitted(self):
        """A pod requesting allocatable - daemonset is the largest pod that can still fit."""
        cfg = make_config(min_nodes=1, taint_rate=1.0)
        ds_pod = PodInfo(
            name="ds",
            namespace="kube-system",
            cpu_request=0.5,
            memory_request=512 * 1024 * 1024,
            node_name="n1",
            is_daemonset=True,
            start_time=NOW,
        )
        node = make_node("n1", cpu=16.0, mem=64 * GiB)
        node.pods = [ds_pod]

        pp = make_pending_pod_mock(cpu="15.5", memory=f"{64 * 1024 - 512}Mi")

        filtered = pending_pods_for_group([pp], [node], cfg.taint_key)
        assert len(filtered) == 1
