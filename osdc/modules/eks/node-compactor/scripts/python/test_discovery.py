"""Unit tests for node discovery and state building."""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from discovery import build_node_states, discover_managed_nodes
from lightkube import ApiError
from models import Config

# ============================================================================
# Helpers
# ============================================================================

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
        "fleet_cooldown": 120,
        "taint_rate": 1.0,
        "spare_capacity_nodes": 2,
        "spare_capacity_ratio": 0.15,
        "spare_capacity_threshold": 0.4,
        "capacity_reservation_nodes": 0,
    }
    defaults.update(overrides)
    return Config(**defaults)


def make_mock_nodepool(name: str, labels: dict | None = None):
    np = MagicMock()
    np.metadata.name = name
    np.metadata.labels = labels or {}
    return np


def make_mock_node(
    name: str,
    labels: dict | None = None,
    cpu: str = "16",
    memory: str = "64Gi",
    creation_timestamp: datetime | None = None,
    taints: list | None = None,
):
    node = MagicMock()
    node.metadata.name = name
    node.metadata.labels = labels or {}
    node.metadata.creationTimestamp = creation_timestamp or NOW - timedelta(hours=1)
    node.status.allocatable = {"cpu": cpu, "memory": memory}
    node.spec.taints = taints or []
    return node


def make_mock_namespace(name: str, deletion_timestamp=None):
    ns = MagicMock()
    ns.metadata.name = name
    ns.metadata.deletionTimestamp = deletion_timestamp
    return ns


def make_mock_pod(
    name: str = "pod-1",
    namespace: str = "default",
    node_name: str | None = "node-1",
    phase: str = "Running",
    cpu_request: str = "1",
    memory_request: str = "4Gi",
    owner_kind: str | None = None,
    start_time: datetime | None = None,
    conditions: list | None = None,
    tolerations: list | None = None,
    creation_timestamp: datetime | None = None,
    scheduling_gates: list | None = None,
    deletion_timestamp: datetime | None = None,
):
    pod = MagicMock()
    pod.metadata.name = name
    pod.metadata.namespace = namespace
    pod.metadata.creationTimestamp = creation_timestamp or NOW - timedelta(minutes=5)
    pod.metadata.deletionTimestamp = deletion_timestamp
    pod.metadata.ownerReferences = []
    if owner_kind:
        ref = MagicMock()
        ref.kind = owner_kind
        pod.metadata.ownerReferences = [ref]
    pod.spec.nodeName = node_name
    pod.spec.schedulingGates = scheduling_gates
    container = MagicMock()
    container.resources.requests = {"cpu": cpu_request, "memory": memory_request}
    pod.spec.containers = [container]
    pod.spec.tolerations = tolerations or []
    pod.status.phase = phase
    pod.status.startTime = start_time
    pod.status.conditions = conditions or []
    return pod


def make_mock_taint(key: str, effect: str = "NoSchedule", value: str = "true"):
    taint = MagicMock()
    taint.key = key
    taint.effect = effect
    taint.value = value
    return taint


def make_api_error(code: int):
    """Create a lightkube ApiError with the given status code."""
    import httpx

    response = httpx.Response(
        code,
        json={
            "kind": "Status",
            "apiVersion": "v1",
            "status": "Failure",
            "message": f"error {code}",
            "code": code,
        },
        request=httpx.Request("GET", "http://test"),
    )
    return ApiError(response=response)


# ============================================================================
# discover_managed_nodes tests
# ============================================================================


class TestDiscoverManagedNodes:
    """Tests for discover_managed_nodes()."""

    @patch("lightkube.generic_resource.create_global_resource")
    def test_no_nodepools_with_label_returns_empty(self, mock_create_resource):
        """NodePools exist but none have the compactor label."""
        cfg = make_config()
        client = MagicMock()

        np1 = make_mock_nodepool("pool-a", labels={"other-label": "true"})
        np2 = make_mock_nodepool("pool-b", labels={})

        mock_nodepool_cls = MagicMock()
        mock_create_resource.return_value = mock_nodepool_cls
        client.list.return_value = [np1, np2]

        result = discover_managed_nodes(client, cfg)

        assert result == {}

    @patch("lightkube.generic_resource.create_global_resource")
    def test_nodepools_with_label_returns_matching_nodes(self, mock_create_resource):
        """Nodes belonging to labeled NodePools are returned."""
        cfg = make_config()
        client = MagicMock()

        np1 = make_mock_nodepool("managed-pool", labels={"osdc.io/node-compactor": "true"})
        np2 = make_mock_nodepool("other-pool", labels={})

        mock_nodepool_cls = MagicMock()
        mock_create_resource.return_value = mock_nodepool_cls

        node1 = make_mock_node("node-1", labels={"karpenter.sh/nodepool": "managed-pool"})
        node2 = make_mock_node("node-2", labels={"karpenter.sh/nodepool": "other-pool"})
        node3 = make_mock_node("node-3", labels={"karpenter.sh/nodepool": "managed-pool"})

        # First call lists NodePools, second call lists Nodes
        client.list.side_effect = [[np1, np2], [node1, node2, node3]]

        result = discover_managed_nodes(client, cfg)

        assert result == {"node-1": "managed-pool", "node-3": "managed-pool"}

    @patch("lightkube.generic_resource.create_global_resource")
    def test_nodes_not_in_managed_pools_excluded(self, mock_create_resource):
        """Nodes in non-labeled pools are excluded."""
        cfg = make_config()
        client = MagicMock()

        np1 = make_mock_nodepool("managed-pool", labels={"osdc.io/node-compactor": "true"})
        mock_nodepool_cls = MagicMock()
        mock_create_resource.return_value = mock_nodepool_cls

        node1 = make_mock_node("node-unmanaged", labels={"karpenter.sh/nodepool": "other-pool"})
        node2 = make_mock_node("node-no-label", labels={})

        client.list.side_effect = [[np1], [node1, node2]]

        result = discover_managed_nodes(client, cfg)

        assert result == {}

    @patch("lightkube.generic_resource.create_global_resource")
    def test_karpenter_crd_404_returns_empty(self, mock_create_resource):
        """404 ApiError (CRD not installed) returns empty dict gracefully."""
        cfg = make_config()
        client = MagicMock()

        mock_nodepool_cls = MagicMock()
        mock_create_resource.return_value = mock_nodepool_cls
        client.list.side_effect = make_api_error(404)

        result = discover_managed_nodes(client, cfg)

        assert result == {}

    @patch("lightkube.generic_resource.create_global_resource")
    def test_non_404_api_error_re_raises(self, mock_create_resource):
        """Non-404 ApiErrors are re-raised."""
        cfg = make_config()
        client = MagicMock()

        mock_nodepool_cls = MagicMock()
        mock_create_resource.return_value = mock_nodepool_cls
        client.list.side_effect = make_api_error(500)

        with pytest.raises(ApiError):
            discover_managed_nodes(client, cfg)

    @patch("lightkube.generic_resource.create_global_resource")
    def test_nodepool_label_value_not_true_excluded(self, mock_create_resource):
        """NodePool with label set to something other than 'true' is excluded."""
        cfg = make_config()
        client = MagicMock()

        np1 = make_mock_nodepool("pool-a", labels={"osdc.io/node-compactor": "false"})
        mock_nodepool_cls = MagicMock()
        mock_create_resource.return_value = mock_nodepool_cls
        client.list.return_value = [np1]

        result = discover_managed_nodes(client, cfg)

        assert result == {}

    @patch("lightkube.generic_resource.create_global_resource")
    def test_multiple_managed_pools(self, mock_create_resource):
        """Multiple labeled pools discover nodes from all of them."""
        cfg = make_config()
        client = MagicMock()

        np1 = make_mock_nodepool("pool-a", labels={"osdc.io/node-compactor": "true"})
        np2 = make_mock_nodepool("pool-b", labels={"osdc.io/node-compactor": "true"})

        mock_nodepool_cls = MagicMock()
        mock_create_resource.return_value = mock_nodepool_cls

        node1 = make_mock_node("n1", labels={"karpenter.sh/nodepool": "pool-a"})
        node2 = make_mock_node("n2", labels={"karpenter.sh/nodepool": "pool-b"})

        client.list.side_effect = [[np1, np2], [node1, node2]]

        result = discover_managed_nodes(client, cfg)

        assert result == {"n1": "pool-a", "n2": "pool-b"}

    @patch("lightkube.generic_resource.create_global_resource")
    def test_nodepool_metadata_none(self, mock_create_resource):
        """NodePool with metadata=None -> labels fallback to {}, skipped."""
        cfg = make_config()
        client = MagicMock()

        np_none = MagicMock()
        np_none.metadata = None

        mock_nodepool_cls = MagicMock()
        mock_create_resource.return_value = mock_nodepool_cls
        client.list.return_value = [np_none]

        result = discover_managed_nodes(client, cfg)

        assert result == {}

    @patch("lightkube.generic_resource.create_global_resource")
    def test_nodepool_metadata_labels_none(self, mock_create_resource):
        """NodePool with metadata.labels=None -> labels fallback to {}."""
        cfg = make_config()
        client = MagicMock()

        np = MagicMock()
        np.metadata.labels = None
        np.metadata.name = "pool-nolabels"

        mock_nodepool_cls = MagicMock()
        mock_create_resource.return_value = mock_nodepool_cls
        client.list.return_value = [np]

        result = discover_managed_nodes(client, cfg)

        assert result == {}

    @patch("lightkube.generic_resource.create_global_resource")
    def test_node_without_nodepool_label(self, mock_create_resource):
        """Node with no karpenter.sh/nodepool label -> not matched."""
        cfg = make_config()
        client = MagicMock()

        np1 = make_mock_nodepool("managed-pool", labels={"osdc.io/node-compactor": "true"})
        mock_nodepool_cls = MagicMock()
        mock_create_resource.return_value = mock_nodepool_cls

        node = make_mock_node("node-nolabel", labels={})

        client.list.side_effect = [[np1], [node]]

        result = discover_managed_nodes(client, cfg)

        assert result == {}

    @patch("lightkube.generic_resource.create_global_resource")
    def test_node_metadata_labels_none_in_discover(self, mock_create_resource):
        """Node with metadata.labels=None in discover -> defaults to {}."""
        cfg = make_config()
        client = MagicMock()

        np1 = make_mock_nodepool("managed-pool", labels={"osdc.io/node-compactor": "true"})
        mock_nodepool_cls = MagicMock()
        mock_create_resource.return_value = mock_nodepool_cls

        node = make_mock_node("node-1")
        node.metadata.labels = None

        client.list.side_effect = [[np1], [node]]

        result = discover_managed_nodes(client, cfg)

        assert result == {}


# ============================================================================
# build_node_states tests
# ============================================================================


class TestBuildNodeStates:
    """Tests for build_node_states()."""

    def test_empty_managed_names_returns_empty(self):
        """Empty managed_node_names returns ({}, [])."""
        cfg = make_config()
        client = MagicMock()

        node_states, pending = build_node_states(client, cfg, {})

        assert node_states == {}
        assert pending == []
        # client.list should not be called at all
        client.list.assert_not_called()

    def test_builds_node_state_correctly(self):
        """NodeState has correct CPU, memory, nodepool, creation time."""
        cfg = make_config()
        client = MagicMock()
        creation = NOW - timedelta(hours=5)

        node = make_mock_node(
            "node-1",
            labels={"karpenter.sh/nodepool": "my-pool"},
            cpu="16",
            memory="64Gi",
            creation_timestamp=creation,
        )

        client.list.side_effect = [[node], [], []]  # Nodes, Namespaces, Pods

        states, pending = build_node_states(client, cfg, {"node-1": "pool"})

        assert "node-1" in states
        ns = states["node-1"]
        assert ns.name == "node-1"
        assert ns.nodepool == "my-pool"
        assert ns.allocatable_cpu == 16.0
        assert ns.allocatable_memory == 64 * GiB
        assert ns.creation_time == creation
        assert ns.is_tainted is False
        assert pending == []

    def test_assigns_pods_to_correct_nodes(self):
        """Pods are assigned to the correct node's NodeState."""
        cfg = make_config()
        client = MagicMock()

        node1 = make_mock_node("node-1", labels={"karpenter.sh/nodepool": "pool"})
        node2 = make_mock_node("node-2", labels={"karpenter.sh/nodepool": "pool"})

        pod1 = make_mock_pod("pod-a", node_name="node-1", cpu_request="2")
        pod2 = make_mock_pod("pod-b", node_name="node-2", cpu_request="4")

        client.list.side_effect = [[node1, node2], [], [pod1, pod2]]

        states, _ = build_node_states(client, cfg, {"node-1": "pool", "node-2": "pool"})

        assert len(states["node-1"].pods) == 1
        assert states["node-1"].pods[0].name == "pod-a"
        assert len(states["node-2"].pods) == 1
        assert states["node-2"].pods[0].name == "pod-b"

    def test_skips_pods_on_non_managed_nodes(self):
        """Pods on nodes not in managed_node_names are ignored."""
        cfg = make_config()
        client = MagicMock()

        node1 = make_mock_node("node-1", labels={"karpenter.sh/nodepool": "pool"})

        pod_managed = make_mock_pod("pod-m", node_name="node-1")
        pod_other = make_mock_pod("pod-other", node_name="node-unmanaged")

        client.list.side_effect = [[node1], [], [pod_managed, pod_other]]

        states, _ = build_node_states(client, cfg, {"node-1": "pool"})

        assert len(states["node-1"].pods) == 1
        assert states["node-1"].pods[0].name == "pod-m"

    def test_skips_succeeded_and_failed_pods(self):
        """Pods in Succeeded or Failed phase are skipped."""
        cfg = make_config()
        client = MagicMock()

        node = make_mock_node("node-1", labels={"karpenter.sh/nodepool": "pool"})

        pod_ok = make_mock_pod("pod-running", node_name="node-1", phase="Running")
        pod_done = make_mock_pod("pod-done", node_name="node-1", phase="Succeeded")
        pod_fail = make_mock_pod("pod-fail", node_name="node-1", phase="Failed")

        client.list.side_effect = [[node], [], [pod_ok, pod_done, pod_fail]]

        states, _ = build_node_states(client, cfg, {"node-1": "pool"})

        assert len(states["node-1"].pods) == 1
        assert states["node-1"].pods[0].name == "pod-running"

    def test_identifies_tainted_nodes(self):
        """Nodes with cfg.taint_key in their taints are marked is_tainted."""
        cfg = make_config()
        client = MagicMock()

        taint = make_mock_taint("node-compactor.osdc.io/consolidating")
        node = make_mock_node(
            "node-1",
            labels={"karpenter.sh/nodepool": "pool"},
            taints=[taint],
        )

        client.list.side_effect = [[node], [], []]

        states, _ = build_node_states(client, cfg, {"node-1": "pool"})

        assert states["node-1"].is_tainted is True

    def test_node_without_compactor_taint_not_tainted(self):
        """Nodes with other taints but not cfg.taint_key are not tainted."""
        cfg = make_config()
        client = MagicMock()

        taint = make_mock_taint("some-other-taint")
        node = make_mock_node(
            "node-1",
            labels={"karpenter.sh/nodepool": "pool"},
            taints=[taint],
        )

        client.list.side_effect = [[node], [], []]

        states, _ = build_node_states(client, cfg, {"node-1": "pool"})

        assert states["node-1"].is_tainted is False

    def test_collects_pending_unschedulable_pods(self):
        """Pending pods with PodScheduled=False/Unschedulable are collected."""
        cfg = make_config()
        client = MagicMock()

        node = make_mock_node("node-1", labels={"karpenter.sh/nodepool": "pool"})

        condition = MagicMock()
        condition.type = "PodScheduled"
        condition.reason = "Unschedulable"
        condition.status = "False"
        condition.message = ""

        pending_pod = make_mock_pod("pending-pod", node_name=None, phase="Pending", conditions=[condition])

        client.list.side_effect = [[node], [], [pending_pod]]

        states, pending = build_node_states(client, cfg, {"node-1": "pool"})

        assert len(pending) == 1
        assert pending[0].metadata.name == "pending-pod"
        assert len(states["node-1"].pods) == 0

    def test_pending_pod_without_unschedulable_still_collected(self):
        """Pending pods without Unschedulable condition ARE now collected (broader detection)."""
        cfg = make_config()
        client = MagicMock()

        node = make_mock_node("node-1", labels={"karpenter.sh/nodepool": "pool"})

        pending_pod = make_mock_pod("pending-ok", node_name=None, phase="Pending", conditions=[])

        client.list.side_effect = [[node], [], [pending_pod]]

        _states, pending = build_node_states(client, cfg, {"node-1": "pool"})

        assert len(pending) == 1
        assert pending[0].metadata.name == "pending-ok"

    def test_daemonset_pod_identified(self):
        """DaemonSet pods are marked is_daemonset=True."""
        cfg = make_config()
        client = MagicMock()

        node = make_mock_node("node-1", labels={"karpenter.sh/nodepool": "pool"})

        ds_pod = make_mock_pod("ds-pod", node_name="node-1", owner_kind="DaemonSet")
        regular_pod = make_mock_pod("regular-pod", node_name="node-1", owner_kind="ReplicaSet")

        client.list.side_effect = [[node], [], [ds_pod, regular_pod]]

        states, _ = build_node_states(client, cfg, {"node-1": "pool"})

        pods = states["node-1"].pods
        assert len(pods) == 2
        ds = next(p for p in pods if p.name == "ds-pod")
        reg = next(p for p in pods if p.name == "regular-pod")
        assert ds.is_daemonset is True
        assert reg.is_daemonset is False

    def test_pod_with_no_spec_skipped(self):
        """Pod with no spec is skipped."""
        cfg = make_config()
        client = MagicMock()

        node = make_mock_node("node-1", labels={"karpenter.sh/nodepool": "pool"})

        pod = MagicMock()
        pod.status.phase = "Running"
        pod.spec = None

        client.list.side_effect = [[node], [], [pod]]

        states, _ = build_node_states(client, cfg, {"node-1": "pool"})

        assert len(states["node-1"].pods) == 0

    def test_pod_with_no_node_name_skipped(self):
        """Pod with no spec.nodeName is skipped."""
        cfg = make_config()
        client = MagicMock()

        node = make_mock_node("node-1", labels={"karpenter.sh/nodepool": "pool"})

        pod = make_mock_pod("pod-no-node", node_name=None, phase="Running")
        # Override: spec exists but nodeName is None
        pod.spec.nodeName = None

        client.list.side_effect = [[node], [], [pod]]

        states, _ = build_node_states(client, cfg, {"node-1": "pool"})

        assert len(states["node-1"].pods) == 0

    def test_node_without_creation_time_defaults_to_now(self):
        """Node with no creationTimestamp defaults to ~now."""
        cfg = make_config()
        client = MagicMock()

        node = make_mock_node(
            "node-1",
            labels={"karpenter.sh/nodepool": "pool"},
            creation_timestamp=None,
        )
        # Override: metadata.creationTimestamp is None
        node.metadata.creationTimestamp = None

        client.list.side_effect = [[node], [], []]

        before = datetime.now(UTC)
        states, _ = build_node_states(client, cfg, {"node-1": "pool"})
        after = datetime.now(UTC)

        ct = states["node-1"].creation_time
        assert before <= ct <= after

    def test_node_not_in_managed_set_skipped(self):
        """Nodes not in managed_node_names are not included in states."""
        cfg = make_config()
        client = MagicMock()

        node1 = make_mock_node("node-managed", labels={"karpenter.sh/nodepool": "pool"})
        node2 = make_mock_node("node-other", labels={"karpenter.sh/nodepool": "pool"})

        client.list.side_effect = [[node1, node2], [], []]

        states, _ = build_node_states(client, cfg, {"node-managed": "pool"})

        assert "node-managed" in states
        assert "node-other" not in states

    def test_nodepool_label_defaults_to_unknown(self):
        """Node without karpenter.sh/nodepool label defaults to 'unknown'."""
        cfg = make_config()
        client = MagicMock()

        node = make_mock_node("node-1", labels={})

        client.list.side_effect = [[node], [], []]

        states, _ = build_node_states(client, cfg, {"node-1": "pool"})

        assert states["node-1"].nodepool == "unknown"

    def test_pod_start_time_preserved(self):
        """Pod start time is carried through to PodInfo."""
        cfg = make_config()
        client = MagicMock()

        node = make_mock_node("node-1", labels={"karpenter.sh/nodepool": "pool"})
        st = NOW - timedelta(minutes=30)
        pod = make_mock_pod("pod-1", node_name="node-1", start_time=st)

        client.list.side_effect = [[node], [], [pod]]

        states, _ = build_node_states(client, cfg, {"node-1": "pool"})

        assert states["node-1"].pods[0].start_time == st

    def test_node_taints_stored_in_state(self):
        """Raw taint objects are stored in NodeState.node_taints."""
        cfg = make_config()
        client = MagicMock()

        t1 = make_mock_taint("taint-a")
        t2 = make_mock_taint("taint-b")
        node = make_mock_node(
            "node-1",
            labels={"karpenter.sh/nodepool": "pool"},
            taints=[t1, t2],
        )

        client.list.side_effect = [[node], [], []]

        states, _ = build_node_states(client, cfg, {"node-1": "pool"})

        assert len(states["node-1"].node_taints) == 2

    def test_node_with_status_none(self):
        """Node with status=None -> allocatable defaults to {}."""
        cfg = make_config()
        client = MagicMock()

        node = make_mock_node("node-1", labels={"karpenter.sh/nodepool": "pool"})
        node.status = None

        client.list.side_effect = [[node], [], []]

        states, _ = build_node_states(client, cfg, {"node-1": "pool"})

        assert states["node-1"].allocatable_cpu == 0.0
        assert states["node-1"].allocatable_memory == 0

    def test_node_with_spec_none(self):
        """Node with spec=None -> taints default to [], is_tainted=False."""
        cfg = make_config()
        client = MagicMock()

        node = make_mock_node("node-1", labels={"karpenter.sh/nodepool": "pool"})
        node.spec = None

        client.list.side_effect = [[node], [], []]

        states, _ = build_node_states(client, cfg, {"node-1": "pool"})

        assert states["node-1"].is_tainted is False
        assert states["node-1"].node_taints == []

    def test_node_with_metadata_labels_none(self):
        """Node with metadata.labels=None -> nodepool defaults to 'unknown'."""
        cfg = make_config()
        client = MagicMock()

        node = make_mock_node("node-1")
        node.metadata.labels = None

        client.list.side_effect = [[node], [], []]

        states, _ = build_node_states(client, cfg, {"node-1": "pool"})

        assert states["node-1"].nodepool == "unknown"

    def test_node_allocatable_none(self):
        """Node with status.allocatable=None -> cpu/memory default to 0."""
        cfg = make_config()
        client = MagicMock()

        node = make_mock_node("node-1", labels={"karpenter.sh/nodepool": "pool"})
        node.status.allocatable = None

        client.list.side_effect = [[node], [], []]

        states, _ = build_node_states(client, cfg, {"node-1": "pool"})

        assert states["node-1"].allocatable_cpu == 0.0
        assert states["node-1"].allocatable_memory == 0

    def test_pod_with_status_none_on_managed_node(self):
        """Pod with status=None on managed node -> phase is None, added with start_time=None."""
        cfg = make_config()
        client = MagicMock()

        node = make_mock_node("node-1", labels={"karpenter.sh/nodepool": "pool"})

        pod = make_mock_pod("pod-nostatus", node_name="node-1")
        pod.status = None

        client.list.side_effect = [[node], [], [pod]]

        states, _ = build_node_states(client, cfg, {"node-1": "pool"})

        assert len(states["node-1"].pods) == 1
        assert states["node-1"].pods[0].start_time is None

    def test_pod_start_time_none(self):
        """Pod with status.startTime=None -> start_time=None in PodInfo."""
        cfg = make_config()
        client = MagicMock()

        node = make_mock_node("node-1", labels={"karpenter.sh/nodepool": "pool"})
        pod = make_mock_pod("pod-nostart", node_name="node-1", start_time=None)
        pod.status.startTime = None

        client.list.side_effect = [[node], [], [pod]]

        states, _ = build_node_states(client, cfg, {"node-1": "pool"})

        assert states["node-1"].pods[0].start_time is None

    def test_pending_pod_with_conditions_none_still_collected(self):
        """Pending pod with conditions=None -> still collected (no exclusion triggered)."""
        cfg = make_config()
        client = MagicMock()

        node = make_mock_node("node-1", labels={"karpenter.sh/nodepool": "pool"})

        pod = make_mock_pod("pending-nocond", node_name=None, phase="Pending")
        pod.status.conditions = None

        client.list.side_effect = [[node], [], [pod]]

        _, pending = build_node_states(client, cfg, {"node-1": "pool"})

        assert len(pending) == 1

    def test_pending_pod_condition_type_not_podscheduled_still_collected(self):
        """Pending pod with non-PodScheduled condition -> still collected (broader detection)."""
        cfg = make_config()
        client = MagicMock()

        node = make_mock_node("node-1", labels={"karpenter.sh/nodepool": "pool"})

        cond = MagicMock()
        cond.type = "Ready"
        cond.reason = "Unschedulable"
        cond.status = "False"
        cond.message = ""

        pod = make_mock_pod("pending-ready", node_name=None, phase="Pending", conditions=[cond])

        client.list.side_effect = [[node], [], [pod]]

        _, pending = build_node_states(client, cfg, {"node-1": "pool"})

        assert len(pending) == 1

    def test_pending_pod_scheduled_but_not_unschedulable_reason_still_collected(self):
        """Pending pod with PodScheduled reason != Unschedulable -> still collected."""
        cfg = make_config()
        client = MagicMock()

        node = make_mock_node("node-1", labels={"karpenter.sh/nodepool": "pool"})

        cond = MagicMock()
        cond.type = "PodScheduled"
        cond.reason = "OtherReason"
        cond.status = "False"
        cond.message = ""

        pod = make_mock_pod("pending-other", node_name=None, phase="Pending", conditions=[cond])

        client.list.side_effect = [[node], [], [pod]]

        _, pending = build_node_states(client, cfg, {"node-1": "pool"})

        assert len(pending) == 1

    def test_pending_pod_scheduled_true_still_collected(self):
        """Pending pod with PodScheduled status=True -> still collected (broader detection)."""
        cfg = make_config()
        client = MagicMock()

        node = make_mock_node("node-1", labels={"karpenter.sh/nodepool": "pool"})

        cond = MagicMock()
        cond.type = "PodScheduled"
        cond.reason = "Unschedulable"
        cond.status = "True"
        cond.message = ""

        pod = make_mock_pod("pending-true", node_name=None, phase="Pending", conditions=[cond])

        client.list.side_effect = [[node], [], [pod]]

        _, pending = build_node_states(client, cfg, {"node-1": "pool"})

        assert len(pending) == 1


# ============================================================================
# Pending pod exclusion filter tests
# ============================================================================


class TestPendingPodExclusionFilters:
    """Tests for the pending pod exclusion filters in build_node_states()."""

    def _run_with_pending_pod(self, pod, namespaces=None):
        """Helper: run build_node_states with one managed node and one pending pod."""
        cfg = make_config()
        client = MagicMock()
        node = make_mock_node("node-1", labels={"karpenter.sh/nodepool": "pool"})
        ns_list = namespaces or []
        client.list.side_effect = [[node], ns_list, [pod]]
        return build_node_states(client, cfg, {"node-1": "pool"})

    def test_scheduling_gates_excluded(self):
        """Pod with scheduling gates is excluded from pending list."""
        gate = MagicMock()
        gate.name = "my-gate"
        pod = make_mock_pod(
            "gated-pod",
            node_name=None,
            phase="Pending",
            scheduling_gates=[gate],
        )
        _, pending = self._run_with_pending_pod(pod)
        assert len(pending) == 0

    def test_empty_scheduling_gates_not_excluded(self):
        """Pod with empty scheduling gates list is NOT excluded."""
        pod = make_mock_pod(
            "ungated-pod",
            node_name=None,
            phase="Pending",
            scheduling_gates=[],
        )
        _, pending = self._run_with_pending_pod(pod)
        assert len(pending) == 1

    def test_daemonset_pending_pod_excluded(self):
        """DaemonSet-owned pending pod is excluded from pending list."""
        pod = make_mock_pod(
            "ds-pending",
            node_name=None,
            phase="Pending",
            owner_kind="DaemonSet",
        )
        _, pending = self._run_with_pending_pod(pod)
        assert len(pending) == 0

    def test_replicaset_pending_pod_not_excluded(self):
        """ReplicaSet-owned pending pod is NOT excluded."""
        pod = make_mock_pod(
            "rs-pending",
            node_name=None,
            phase="Pending",
            owner_kind="ReplicaSet",
        )
        _, pending = self._run_with_pending_pod(pod)
        assert len(pending) == 1

    def test_pod_too_young_excluded(self):
        """Pod pending for only 10s is excluded (< 30s threshold)."""
        pod = make_mock_pod(
            "young-pod",
            node_name=None,
            phase="Pending",
            creation_timestamp=NOW - timedelta(seconds=10),
        )
        _, pending = self._run_with_pending_pod(pod)
        assert len(pending) == 0

    def test_pod_old_enough_included(self):
        """Pod pending for 60s is included (> 30s threshold)."""
        pod = make_mock_pod(
            "old-pod",
            node_name=None,
            phase="Pending",
            creation_timestamp=NOW - timedelta(seconds=60),
        )
        _, pending = self._run_with_pending_pod(pod)
        assert len(pending) == 1

    def test_pod_exactly_at_threshold_excluded(self):
        """Pod pending for exactly 30s is excluded (< not <=)."""
        # The check is `age < 30`, so exactly 30 should pass
        pod = make_mock_pod(
            "threshold-pod",
            node_name=None,
            phase="Pending",
            creation_timestamp=NOW - timedelta(seconds=30),
        )
        _, pending = self._run_with_pending_pod(pod)
        assert len(pending) == 1

    def test_terminating_namespace_excluded(self):
        """Pod in a namespace with deletionTimestamp is excluded."""
        ns = make_mock_namespace("dying-ns", deletion_timestamp=NOW)
        pod = make_mock_pod(
            "dying-pod",
            namespace="dying-ns",
            node_name=None,
            phase="Pending",
        )
        _, pending = self._run_with_pending_pod(pod, namespaces=[ns])
        assert len(pending) == 0

    def test_active_namespace_not_excluded(self):
        """Pod in a namespace without deletionTimestamp is NOT excluded."""
        ns = make_mock_namespace("active-ns", deletion_timestamp=None)
        pod = make_mock_pod(
            "active-pod",
            namespace="active-ns",
            node_name=None,
            phase="Pending",
        )
        _, pending = self._run_with_pending_pod(pod, namespaces=[ns])
        assert len(pending) == 1

    def test_volume_binding_pvc_excluded(self):
        """Pod waiting for PVC binding is excluded."""
        cond = MagicMock()
        cond.type = "PodScheduled"
        cond.status = "False"
        cond.reason = "Unschedulable"
        cond.message = "0/10 nodes available: 1 persistentvolumeclaim not found"

        pod = make_mock_pod(
            "pvc-pod",
            node_name=None,
            phase="Pending",
            conditions=[cond],
        )
        _, pending = self._run_with_pending_pod(pod)
        assert len(pending) == 0

    def test_volume_binding_bound_excluded(self):
        """Pod waiting for volume bound is excluded."""
        cond = MagicMock()
        cond.type = "PodScheduled"
        cond.status = "False"
        cond.reason = "Unschedulable"
        cond.message = "0/5 nodes available: 2 node(s) didn't find available persistent volumes, volume not bound"

        pod = make_mock_pod(
            "bound-pod",
            node_name=None,
            phase="Pending",
            conditions=[cond],
        )
        _, pending = self._run_with_pending_pod(pod)
        assert len(pending) == 0

    def test_normal_unschedulable_not_excluded_by_volume_filter(self):
        """Normal unschedulable pod (no volume message) is NOT excluded."""
        cond = MagicMock()
        cond.type = "PodScheduled"
        cond.status = "False"
        cond.reason = "Unschedulable"
        cond.message = "0/10 nodes available: insufficient cpu"

        pod = make_mock_pod(
            "cpu-pod",
            node_name=None,
            phase="Pending",
            conditions=[cond],
        )
        _, pending = self._run_with_pending_pod(pod)
        assert len(pending) == 1

    def test_normal_pending_pod_included(self):
        """A normal pending pod without nodeName passes all filters."""
        pod = make_mock_pod(
            "normal-pending",
            node_name=None,
            phase="Pending",
        )
        _, pending = self._run_with_pending_pod(pod)
        assert len(pending) == 1
        assert pending[0].metadata.name == "normal-pending"

    def test_pending_pod_with_node_name_not_collected(self):
        """A pending pod that already has a nodeName is not collected."""
        pod = make_mock_pod(
            "assigned-pending",
            node_name="node-1",
            phase="Pending",
        )
        _, pending = self._run_with_pending_pod(pod)
        assert len(pending) == 0

    def test_backward_compat_unschedulable_still_collected(self):
        """Backward compat: PodScheduled=False/Unschedulable IS still collected."""
        cond = MagicMock()
        cond.type = "PodScheduled"
        cond.reason = "Unschedulable"
        cond.status = "False"
        cond.message = "0/10 nodes available: insufficient cpu"

        pod = make_mock_pod(
            "unschedulable-pod",
            node_name=None,
            phase="Pending",
            conditions=[cond],
        )
        _, pending = self._run_with_pending_pod(pod)
        assert len(pending) == 1
        assert pending[0].metadata.name == "unschedulable-pod"


# ============================================================================
# Terminating pod filtering tests
# ============================================================================


class TestTerminatingPodFiltering:
    """Tests for filtering pods with deletionTimestamp set (Terminating pods)."""

    def test_terminating_running_pod_excluded_from_node(self):
        """Running pod with deletionTimestamp set is excluded from node's pod list."""
        cfg = make_config()
        client = MagicMock()

        node = make_mock_node("node-1", labels={"karpenter.sh/nodepool": "pool"})

        running_pod = make_mock_pod("running-pod", node_name="node-1", phase="Running")
        terminating_pod = make_mock_pod(
            "terminating-pod",
            node_name="node-1",
            phase="Running",
            deletion_timestamp=NOW,
        )

        client.list.side_effect = [[node], [], [running_pod, terminating_pod]]

        states, _ = build_node_states(client, cfg, {"node-1": "pool"})

        assert len(states["node-1"].pods) == 1
        assert states["node-1"].pods[0].name == "running-pod"

    def test_terminating_daemonset_pod_excluded_from_node(self):
        """DaemonSet pod with deletionTimestamp set is also excluded."""
        cfg = make_config()
        client = MagicMock()

        node = make_mock_node("node-1", labels={"karpenter.sh/nodepool": "pool"})

        ds_pod = make_mock_pod(
            "ds-terminating",
            node_name="node-1",
            phase="Running",
            owner_kind="DaemonSet",
            deletion_timestamp=NOW,
        )

        client.list.side_effect = [[node], [], [ds_pod]]

        states, _ = build_node_states(client, cfg, {"node-1": "pool"})

        assert len(states["node-1"].pods) == 0

    def test_non_terminating_pod_still_included(self):
        """Pod without deletionTimestamp is still included normally."""
        cfg = make_config()
        client = MagicMock()

        node = make_mock_node("node-1", labels={"karpenter.sh/nodepool": "pool"})

        pod = make_mock_pod("normal-pod", node_name="node-1", phase="Running")

        client.list.side_effect = [[node], [], [pod]]

        states, _ = build_node_states(client, cfg, {"node-1": "pool"})

        assert len(states["node-1"].pods) == 1
        assert states["node-1"].pods[0].name == "normal-pod"

    def test_terminating_pending_pod_excluded_from_pending_list(self):
        """Pending pod with deletionTimestamp set is excluded from pending list."""
        cfg = make_config()
        client = MagicMock()

        node = make_mock_node("node-1", labels={"karpenter.sh/nodepool": "pool"})

        pending_pod = make_mock_pod(
            "terminating-pending",
            node_name=None,
            phase="Pending",
            deletion_timestamp=NOW,
        )

        client.list.side_effect = [[node], [], [pending_pod]]

        _, pending = build_node_states(client, cfg, {"node-1": "pool"})

        assert len(pending) == 0

    def test_mix_of_terminating_and_active_pods(self):
        """Only non-terminating pods are counted in node state."""
        cfg = make_config()
        client = MagicMock()

        node = make_mock_node("node-1", labels={"karpenter.sh/nodepool": "pool"})

        active_pod = make_mock_pod("active", node_name="node-1", phase="Running")
        term_pod1 = make_mock_pod("term-1", node_name="node-1", phase="Running", deletion_timestamp=NOW)
        term_pod2 = make_mock_pod(
            "term-2", node_name="node-1", phase="Running", deletion_timestamp=NOW - timedelta(seconds=30)
        )

        client.list.side_effect = [[node], [], [active_pod, term_pod1, term_pod2]]

        states, _ = build_node_states(client, cfg, {"node-1": "pool"})

        assert len(states["node-1"].pods) == 1
        assert states["node-1"].pods[0].name == "active"
