"""Tests for numa_cordon module."""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, call, patch

import pytest
from lightkube.core.exceptions import ApiError
from numa_cordon import (
    CORDON_ANNOTATION,
    cleanup_failed_pods,
    cordon_nodes,
    find_topology_failed_pods,
    get_config,
    main,
    uncordon_drained_nodes,
)


def _make_pod(
    name: str,
    phase: str = "Running",
    reason: str | None = None,
    node_name: str | None = None,
    gpu: int = 0,
):
    pod = MagicMock()
    pod.metadata.name = name
    pod.status.phase = phase
    pod.status.reason = reason
    pod.spec.nodeName = node_name

    if gpu > 0:
        container = MagicMock()
        container.resources.limits = {"nvidia.com/gpu": str(gpu)}
        pod.spec.containers = [container]
    else:
        container = MagicMock()
        container.resources.limits = {"cpu": "1"}
        pod.spec.containers = [container]

    return pod


def _make_node(name: str, unschedulable: bool = False, cordoned_by_us: bool = False):
    node = MagicMock()
    node.metadata.name = name
    node.spec.unschedulable = unschedulable
    annotations = {}
    if cordoned_by_us:
        annotations[CORDON_ANNOTATION] = datetime.now(UTC).isoformat()
    node.metadata.annotations = annotations
    return node


# --- find_topology_failed_pods ---


class TestFindTopologyFailedPods:
    def test_no_pods(self):
        client = MagicMock()
        client.list.return_value = []
        assert find_topology_failed_pods(client, "arc-runners") == []

    def test_finds_topology_error_pods(self):
        client = MagicMock()
        client.list.return_value = [
            _make_pod("good-pod", phase="Running"),
            _make_pod(
                "topo-fail",
                phase="Failed",
                reason="TopologyAffinityError",
                node_name="node-1",
            ),
            _make_pod("other-fail", phase="Failed", reason="OOMKilled"),
        ]
        result = find_topology_failed_pods(client, "arc-runners")
        assert len(result) == 1
        assert result[0].metadata.name == "topo-fail"

    def test_skips_non_failed_phases(self):
        client = MagicMock()
        client.list.return_value = [
            _make_pod("pending", phase="Pending"),
            _make_pod("running", phase="Running"),
            _make_pod("succeeded", phase="Succeeded"),
        ]
        assert find_topology_failed_pods(client, "arc-runners") == []

    def test_skips_pods_without_status(self):
        client = MagicMock()
        pod = _make_pod("no-status")
        pod.status = None
        client.list.return_value = [pod]
        assert find_topology_failed_pods(client, "arc-runners") == []

    def test_multiple_topology_errors(self):
        client = MagicMock()
        client.list.return_value = [
            _make_pod("fail-1", phase="Failed", reason="TopologyAffinityError", node_name="n1"),
            _make_pod("fail-2", phase="Failed", reason="TopologyAffinityError", node_name="n1"),
            _make_pod("fail-3", phase="Failed", reason="TopologyAffinityError", node_name="n2"),
        ]
        result = find_topology_failed_pods(client, "arc-runners")
        assert len(result) == 3


# --- cordon_nodes ---


class TestCordonNodes:
    def test_cordons_node(self):
        client = MagicMock()
        node = _make_node("node-1", unschedulable=False)
        client.get.return_value = node
        pods = [
            _make_pod("fail-1", phase="Failed", reason="TopologyAffinityError", node_name="node-1")
        ]

        cordoned, ok, fail = cordon_nodes(client, pods, dry_run=False)
        assert cordoned == {"node-1"}
        assert ok == 1
        assert fail == 0
        client.patch.assert_called_once()

    def test_skips_already_unschedulable(self):
        client = MagicMock()
        node = _make_node("node-1", unschedulable=True)
        client.get.return_value = node
        pods = [
            _make_pod("fail-1", phase="Failed", reason="TopologyAffinityError", node_name="node-1")
        ]

        cordoned, ok, fail = cordon_nodes(client, pods, dry_run=False)
        assert cordoned == {"node-1"}
        assert ok == 1
        assert fail == 0
        client.patch.assert_not_called()

    def test_dry_run_does_not_patch(self):
        client = MagicMock()
        node = _make_node("node-1", unschedulable=False)
        client.get.return_value = node
        pods = [
            _make_pod("fail-1", phase="Failed", reason="TopologyAffinityError", node_name="node-1")
        ]

        cordoned, ok, fail = cordon_nodes(client, pods, dry_run=True)
        assert cordoned == {"node-1"}
        assert ok == 1
        assert fail == 0
        client.patch.assert_not_called()

    def test_deduplicates_nodes(self):
        client = MagicMock()
        node = _make_node("node-1", unschedulable=False)
        client.get.return_value = node
        pods = [
            _make_pod("f1", phase="Failed", reason="TopologyAffinityError", node_name="node-1"),
            _make_pod("f2", phase="Failed", reason="TopologyAffinityError", node_name="node-1"),
        ]

        cordoned, ok, fail = cordon_nodes(client, pods, dry_run=False)
        assert cordoned == {"node-1"}
        assert ok == 1
        # Only one patch call for the node, not two.
        assert client.patch.call_count == 1

    def test_handles_node_not_found(self):
        client = MagicMock()
        not_found = ApiError.__new__(ApiError)
        not_found.status = MagicMock(code=404)
        client.get.side_effect = not_found
        pods = [
            _make_pod("f1", phase="Failed", reason="TopologyAffinityError", node_name="gone-node")
        ]

        cordoned, ok, fail = cordon_nodes(client, pods, dry_run=False)
        assert cordoned == set()
        assert ok == 0
        assert fail == 0

    def test_handles_patch_failure(self):
        client = MagicMock()
        node = _make_node("node-1", unschedulable=False)
        client.get.return_value = node
        client.patch.side_effect = Exception("API error")
        pods = [
            _make_pod("f1", phase="Failed", reason="TopologyAffinityError", node_name="node-1")
        ]

        cordoned, ok, fail = cordon_nodes(client, pods, dry_run=False)
        assert fail == 1

    def test_skips_pod_without_node_name(self):
        client = MagicMock()
        pods = [
            _make_pod("f1", phase="Failed", reason="TopologyAffinityError", node_name=None)
        ]

        cordoned, ok, fail = cordon_nodes(client, pods, dry_run=False)
        assert cordoned == set()
        assert ok == 0
        assert fail == 0
        client.get.assert_not_called()

    def test_handles_get_node_api_error(self):
        client = MagicMock()
        forbidden = ApiError.__new__(ApiError)
        forbidden.status = MagicMock(code=403)
        client.get.side_effect = forbidden
        pods = [
            _make_pod("f1", phase="Failed", reason="TopologyAffinityError", node_name="node-1")
        ]

        cordoned, ok, fail = cordon_nodes(client, pods, dry_run=False)
        assert fail == 1


# --- cleanup_failed_pods ---


class TestCleanupFailedPods:
    def test_deletes_pods_on_cordoned_nodes(self):
        client = MagicMock()
        pods = [
            _make_pod("f1", phase="Failed", reason="TopologyAffinityError", node_name="node-1"),
            _make_pod("f2", phase="Failed", reason="TopologyAffinityError", node_name="node-2"),
        ]

        deleted, failed = cleanup_failed_pods(
            client, pods, {"node-1", "node-2"}, "arc-runners", dry_run=False
        )
        assert deleted == 2
        assert failed == 0
        assert client.delete.call_count == 2

    def test_skips_pods_not_on_cordoned_nodes(self):
        client = MagicMock()
        pods = [
            _make_pod("f1", phase="Failed", reason="TopologyAffinityError", node_name="other-node"),
        ]

        deleted, failed = cleanup_failed_pods(
            client, pods, {"node-1"}, "arc-runners", dry_run=False
        )
        assert deleted == 0
        assert failed == 0
        client.delete.assert_not_called()

    def test_dry_run(self):
        client = MagicMock()
        pods = [
            _make_pod("f1", phase="Failed", reason="TopologyAffinityError", node_name="node-1"),
        ]

        deleted, failed = cleanup_failed_pods(
            client, pods, {"node-1"}, "arc-runners", dry_run=True
        )
        assert deleted == 1
        assert failed == 0
        client.delete.assert_not_called()

    def test_404_counted_as_success(self):
        client = MagicMock()
        not_found = ApiError.__new__(ApiError)
        not_found.status = MagicMock(code=404)
        client.delete.side_effect = not_found
        pods = [
            _make_pod("gone", phase="Failed", reason="TopologyAffinityError", node_name="node-1"),
        ]

        deleted, failed = cleanup_failed_pods(
            client, pods, {"node-1"}, "arc-runners", dry_run=False
        )
        assert deleted == 1
        assert failed == 0

    def test_api_error_non_404(self):
        client = MagicMock()
        forbidden = ApiError.__new__(ApiError)
        forbidden.status = MagicMock(code=403)
        client.delete.side_effect = forbidden
        pods = [
            _make_pod("f1", phase="Failed", reason="TopologyAffinityError", node_name="node-1"),
        ]

        deleted, failed = cleanup_failed_pods(
            client, pods, {"node-1"}, "arc-runners", dry_run=False
        )
        assert deleted == 0
        assert failed == 1


# --- uncordon_drained_nodes ---


class TestUncordonDrainedNodes:
    def test_uncordons_node_with_no_gpu_pods(self):
        client = MagicMock()
        node = _make_node("node-1", unschedulable=True, cordoned_by_us=True)
        client.list.side_effect = [
            [node],  # Node list
            [],  # Pod list for node-1 (no pods)
        ]

        ok, fail = uncordon_drained_nodes(client, dry_run=False)
        assert ok == 1
        assert fail == 0
        client.patch.assert_called_once()

    def test_keeps_cordon_when_gpu_pods_remain(self):
        client = MagicMock()
        node = _make_node("node-1", unschedulable=True, cordoned_by_us=True)
        gpu_pod = _make_pod("gpu-runner", phase="Running", gpu=4)
        client.list.side_effect = [
            [node],  # Node list
            [gpu_pod],  # Pod list for node-1
        ]

        ok, fail = uncordon_drained_nodes(client, dry_run=False)
        assert ok == 0
        assert fail == 0
        client.patch.assert_not_called()

    def test_ignores_nodes_not_cordoned_by_us(self):
        client = MagicMock()
        node = _make_node("node-1", unschedulable=True, cordoned_by_us=False)
        client.list.return_value = [node]

        ok, fail = uncordon_drained_nodes(client, dry_run=False)
        assert ok == 0
        assert fail == 0

    def test_cleans_annotation_on_already_schedulable_node(self):
        client = MagicMock()
        node = _make_node("node-1", unschedulable=False, cordoned_by_us=True)
        client.list.return_value = [node]

        ok, fail = uncordon_drained_nodes(client, dry_run=False)
        assert ok == 0
        assert fail == 0
        # Should patch to remove annotation.
        client.patch.assert_called_once()

    def test_dry_run(self):
        client = MagicMock()
        node = _make_node("node-1", unschedulable=True, cordoned_by_us=True)
        client.list.side_effect = [
            [node],
            [],  # No pods
        ]

        ok, fail = uncordon_drained_nodes(client, dry_run=True)
        assert ok == 1
        assert fail == 0
        client.patch.assert_not_called()

    def test_ignores_completed_gpu_pods(self):
        client = MagicMock()
        node = _make_node("node-1", unschedulable=True, cordoned_by_us=True)
        done_pod = _make_pod("done-gpu", phase="Succeeded", gpu=4)
        client.list.side_effect = [
            [node],
            [done_pod],  # Only completed GPU pods
        ]

        ok, fail = uncordon_drained_nodes(client, dry_run=False)
        assert ok == 1
        assert fail == 0

    def test_handles_uncordon_failure(self):
        client = MagicMock()
        node = _make_node("node-1", unschedulable=True, cordoned_by_us=True)
        client.list.side_effect = [
            [node],
            [],
        ]
        client.patch.side_effect = Exception("API error")

        ok, fail = uncordon_drained_nodes(client, dry_run=False)
        assert ok == 0
        assert fail == 1


# --- get_config ---


class TestGetConfig:
    def test_defaults(self):
        with patch.dict("os.environ", {}, clear=True):
            config = get_config()
        assert config["namespace"] == "arc-runners"
        assert config["dry_run"] is False
        assert config["uncordon_enabled"] is True

    def test_custom_values(self):
        env = {
            "TARGET_NAMESPACE": "custom-ns",
            "DRY_RUN": "true",
            "UNCORDON_ENABLED": "false",
        }
        with patch.dict("os.environ", env, clear=True):
            config = get_config()
        assert config["namespace"] == "custom-ns"
        assert config["dry_run"] is True
        assert config["uncordon_enabled"] is False


# --- main ---


class TestMain:
    def test_no_failed_pods(self):
        with (
            patch("numa_cordon.Client") as mock_cls,
            patch.dict("os.environ", {}, clear=True),
        ):
            mock_client = MagicMock()
            mock_client.list.return_value = []
            mock_cls.return_value = mock_client

            assert main() == 0

    def test_cordons_and_cleans_up(self):
        pod = _make_pod(
            "fail-1",
            phase="Failed",
            reason="TopologyAffinityError",
            node_name="node-1",
        )
        node = _make_node("node-1", unschedulable=False)

        with (
            patch("numa_cordon.Client") as mock_cls,
            patch.dict("os.environ", {}, clear=True),
        ):
            mock_client = MagicMock()
            # 1st list: find_topology_failed_pods, 2nd list: uncordon_drained_nodes (nodes),
            # 3rd list: uncordon_drained_nodes (pods on node)
            mock_client.list.side_effect = [
                [pod],  # find_topology_failed_pods
                [],  # uncordon_drained_nodes: no nodes with our annotation
            ]
            mock_client.get.return_value = node
            mock_cls.return_value = mock_client

            assert main() == 0
            # patch called for cordon
            assert mock_client.patch.call_count >= 1
            # delete called for failed pod cleanup
            assert mock_client.delete.call_count == 1

    def test_returns_1_on_failure(self):
        with (
            patch("numa_cordon.Client") as mock_cls,
            patch.dict("os.environ", {}, clear=True),
        ):
            mock_client = MagicMock()
            mock_client.list.side_effect = Exception("boom")
            mock_cls.return_value = mock_client

            assert main() == 1
