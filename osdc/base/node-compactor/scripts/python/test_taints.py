"""Tests for taints.py — taint management and pending pod detection."""

import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

from lightkube import ApiError
from lightkube.resources.core_v1 import Node
from lightkube.types import PatchType
from models import Config, NodeState, PodInfo
from taints import (
    _pod_matches_node,
    apply_taint,
    check_pending_pods,
    cleanup_stale_taints,
    remove_taint,
)

NOW = datetime.now(UTC)
GiB = 1024**3


def make_config(**overrides):
    defaults = {
        "interval": 20,
        "max_uptime_hours": 48,
        "nodepool_label": "osdc.io/node-compactor",
        "taint_key": "node-compactor.osdc.io/consolidating",
        "min_nodes": 1,
        "dry_run": False,
        "taint_cooldown": 300,
        "min_node_age": 900,
    }
    defaults.update(overrides)
    return Config(**defaults)


def make_taint(key="some-taint", value="some-value", effect="NoSchedule"):
    t = MagicMock()
    t.key = key
    t.value = value
    t.effect = effect
    return t


def make_toleration(key=None, operator="Equal", value=None, effect=None):
    tol = MagicMock()
    tol.key = key
    tol.operator = operator
    tol.value = value
    tol.effect = effect
    return tol


def make_pod(tolerations=None, cpu="1", memory="1Gi", has_spec=True):
    pod = MagicMock()
    if not has_spec:
        pod.spec = None
        return pod
    pod.spec.tolerations = tolerations
    container = MagicMock()
    container.resources.requests = {"cpu": cpu, "memory": memory}
    pod.spec.containers = [container]
    return pod


def make_node_state(
    name="node-1",
    nodepool="pool-1",
    is_tainted=False,
    node_taints=None,
    allocatable_cpu=8.0,
    allocatable_memory=32 * GiB,
    pods=None,
):
    return NodeState(
        name=name,
        nodepool=nodepool,
        allocatable_cpu=allocatable_cpu,
        allocatable_memory=allocatable_memory,
        creation_time=NOW - timedelta(hours=24),
        pods=pods or [],
        is_tainted=is_tainted,
        node_taints=node_taints or [],
    )


# ============================================================================
# _pod_matches_node tests
# ============================================================================


class TestPodMatchesNode(unittest.TestCase):
    def setUp(self):
        self.node = make_node_state()

    def test_no_taints_matches_all(self):
        pod = make_pod()
        self.assertTrue(_pod_matches_node(pod, self.node, []))

    def test_no_tolerations_with_taints_fails(self):
        pod = make_pod(tolerations=None)
        taint = make_taint()
        self.assertFalse(_pod_matches_node(pod, self.node, [taint]))

    def test_pod_no_spec_with_taints_fails(self):
        pod = make_pod(has_spec=False)
        taint = make_taint()
        self.assertFalse(_pod_matches_node(pod, self.node, [taint]))

    def test_matching_key_value_toleration(self):
        taint = make_taint(key="k1", value="v1", effect="NoSchedule")
        tol = make_toleration(key="k1", operator="Equal", value="v1", effect="NoSchedule")
        pod = make_pod(tolerations=[tol])
        self.assertTrue(_pod_matches_node(pod, self.node, [taint]))

    def test_exists_operator_no_key_wildcard(self):
        taint = make_taint(key="anything", value="val", effect="NoSchedule")
        tol = make_toleration(key=None, operator="Exists")
        pod = make_pod(tolerations=[tol])
        self.assertTrue(_pod_matches_node(pod, self.node, [taint]))

    def test_exists_operator_specific_key(self):
        taint = make_taint(key="k1", value="v1", effect="NoSchedule")
        tol = make_toleration(key="k1", operator="Exists")
        pod = make_pod(tolerations=[tol])
        self.assertTrue(_pod_matches_node(pod, self.node, [taint]))

    def test_effect_mismatch_continues(self):
        """Toleration effect doesn't match taint effect; should continue to next toleration."""
        taint = make_taint(key="k1", value="v1", effect="NoSchedule")
        # First toleration: right key but wrong effect
        tol_wrong_effect = make_toleration(key="k1", operator="Equal", value="v1", effect="NoExecute")
        # Second toleration: right key, right effect
        tol_correct = make_toleration(key="k1", operator="Equal", value="v1", effect="NoSchedule")
        pod = make_pod(tolerations=[tol_wrong_effect, tol_correct])
        self.assertTrue(_pod_matches_node(pod, self.node, [taint]))

    def test_effect_mismatch_only_toleration_fails(self):
        taint = make_taint(key="k1", value="v1", effect="NoSchedule")
        tol = make_toleration(key="k1", operator="Equal", value="v1", effect="NoExecute")
        pod = make_pod(tolerations=[tol])
        self.assertFalse(_pod_matches_node(pod, self.node, [taint]))

    def test_value_mismatch(self):
        taint = make_taint(key="k1", value="v1", effect="NoSchedule")
        tol = make_toleration(key="k1", operator="Equal", value="wrong", effect="NoSchedule")
        pod = make_pod(tolerations=[tol])
        self.assertFalse(_pod_matches_node(pod, self.node, [taint]))

    def test_toleration_no_effect_matches_any_effect(self):
        """Toleration with no effect (None/empty) matches taint regardless of effect."""
        taint = make_taint(key="k1", value="v1", effect="NoSchedule")
        tol = make_toleration(key="k1", operator="Equal", value="v1", effect=None)
        pod = make_pod(tolerations=[tol])
        self.assertTrue(_pod_matches_node(pod, self.node, [taint]))

    def test_multiple_taints_all_tolerated(self):
        taints = [
            make_taint(key="k1", value="v1"),
            make_taint(key="k2", value="v2"),
        ]
        tols = [
            make_toleration(key="k1", operator="Equal", value="v1", effect="NoSchedule"),
            make_toleration(key="k2", operator="Equal", value="v2", effect="NoSchedule"),
        ]
        pod = make_pod(tolerations=tols)
        self.assertTrue(_pod_matches_node(pod, self.node, taints))

    def test_multiple_taints_one_not_tolerated(self):
        taints = [
            make_taint(key="k1", value="v1"),
            make_taint(key="k2", value="v2"),
        ]
        tols = [
            make_toleration(key="k1", operator="Equal", value="v1", effect="NoSchedule"),
        ]
        pod = make_pod(tolerations=tols)
        self.assertFalse(_pod_matches_node(pod, self.node, taints))


# ============================================================================
# check_pending_pods tests
# ============================================================================


class TestCheckPendingPods(unittest.TestCase):
    def setUp(self):
        self.cfg = make_config()

    def test_no_pending_pods(self):
        result = check_pending_pods(self.cfg, {"n": make_node_state(is_tainted=True)}, [])
        self.assertEqual(result, set())

    def test_no_tainted_nodes(self):
        ns = make_node_state(is_tainted=False)
        pod = make_pod()
        result = check_pending_pods(self.cfg, {"n": ns}, [pod])
        self.assertEqual(result, set())

    def test_pending_pods_dont_match_tainted_nodes(self):
        """Pending pods have taints they can't tolerate."""
        other_taint = make_taint(key="special", value="yes", effect="NoSchedule")
        compactor_taint = make_taint(key=self.cfg.taint_key, value="true", effect="NoSchedule")
        ns = make_node_state(
            is_tainted=True,
            node_taints=[compactor_taint, other_taint],
        )
        pod = make_pod(tolerations=None)
        result = check_pending_pods(self.cfg, {"n": ns}, [pod])
        self.assertEqual(result, set())

    def test_compatible_pending_one_node_enough(self):
        compactor_taint = make_taint(key=self.cfg.taint_key, value="true", effect="NoSchedule")
        ns = make_node_state(
            name="node-a",
            is_tainted=True,
            node_taints=[compactor_taint],
            allocatable_cpu=8.0,
            allocatable_memory=32 * GiB,
            pods=[],  # all capacity free
        )
        pod = make_pod(cpu="1", memory="1Gi")
        result = check_pending_pods(self.cfg, {"node-a": ns}, [pod])
        self.assertEqual(result, {"node-a"})

    def test_multiple_pending_need_multiple_nodes(self):
        compactor_taint = make_taint(key=self.cfg.taint_key, value="true", effect="NoSchedule")
        # Node A: 2 CPU free, 4 GiB free
        ns_a = make_node_state(
            name="node-a",
            is_tainted=True,
            node_taints=[compactor_taint],
            allocatable_cpu=4.0,
            allocatable_memory=8 * GiB,
            pods=[
                PodInfo("p", "ns", 2.0, 4 * GiB, "node-a", False, NOW),
            ],
        )
        # Node B: 2 CPU free, 4 GiB free
        ns_b = make_node_state(
            name="node-b",
            is_tainted=True,
            node_taints=[compactor_taint],
            allocatable_cpu=4.0,
            allocatable_memory=8 * GiB,
            pods=[
                PodInfo("p2", "ns", 2.0, 4 * GiB, "node-b", False, NOW),
            ],
        )
        # Total demand: 3 CPU, 6 GiB -- needs both nodes
        pods = [
            make_pod(cpu="2", memory="4Gi"),
            make_pod(cpu="1", memory="2Gi"),
        ]
        result = check_pending_pods(self.cfg, {"node-a": ns_a, "node-b": ns_b}, pods)
        self.assertEqual(result, {"node-a", "node-b"})

    def test_sort_by_utilization_highest_first(self):
        """Highest utilization nodes should be untainted first."""
        compactor_taint = make_taint(key=self.cfg.taint_key, value="true", effect="NoSchedule")
        # Node A: low utilization (25% CPU used)
        ns_a = make_node_state(
            name="node-a",
            is_tainted=True,
            node_taints=[compactor_taint],
            allocatable_cpu=8.0,
            allocatable_memory=32 * GiB,
            pods=[PodInfo("p1", "ns", 2.0, 1 * GiB, "node-a", False, NOW)],
        )
        # Node B: high utilization (75% CPU used)
        ns_b = make_node_state(
            name="node-b",
            is_tainted=True,
            node_taints=[compactor_taint],
            allocatable_cpu=8.0,
            allocatable_memory=32 * GiB,
            pods=[PodInfo("p2", "ns", 6.0, 1 * GiB, "node-b", False, NOW)],
        )
        # Small demand: 1 CPU -- only need 1 node
        pod = make_pod(cpu="1", memory="1Gi")
        result = check_pending_pods(self.cfg, {"node-a": ns_a, "node-b": ns_b}, [pod])
        # Should pick node-b (higher utilization = least wasteful)
        self.assertEqual(result, {"node-b"})

    def test_compatible_tainted_none_match_pending(self):
        """Edge case: compatible_pending exists but after re-filter, no tainted
        nodes match. This covers lines 96-97 (the second compatibility check).

        We patch _pod_matches_node so the first pass (building compatible_pending)
        finds a match, but the second pass (filtering compatible_tainted) does not.
        """
        cfg = make_config()
        compactor_taint = make_taint(key=cfg.taint_key, value="true", effect="NoSchedule")

        ns_a = make_node_state(
            name="node-a",
            is_tainted=True,
            node_taints=[compactor_taint],
        )

        pod = make_pod(tolerations=None)

        # Track which pass we're in via call count.
        # First pass (building compatible_pending): 1 pod x 1 node = call 1 -> True
        # Second pass (filtering compatible_tainted): 1 node x 1 pod = call 2 -> False
        call_count = {"n": 0}

        def controlled_match(p, node, taints):
            call_count["n"] += 1
            return call_count["n"] == 1

        with patch("taints._pod_matches_node", side_effect=controlled_match):
            result = check_pending_pods(cfg, {"node-a": ns_a}, [pod])
        self.assertEqual(result, set())


# ============================================================================
# apply_taint tests
# ============================================================================


class TestApplyTaint(unittest.TestCase):
    def test_normal_apply(self):
        client = MagicMock()
        apply_taint(client, "node-1", "my-taint", dry_run=False)
        client.patch.assert_called_once_with(
            Node,
            "node-1",
            {
                "spec": {
                    "taints": [
                        {
                            "key": "my-taint",
                            "value": "true",
                            "effect": "NoSchedule",
                        }
                    ]
                }
            },
            patch_type=PatchType.STRATEGIC,
        )

    def test_dry_run(self):
        client = MagicMock()
        apply_taint(client, "node-1", "my-taint", dry_run=True)
        client.patch.assert_not_called()


# ============================================================================
# remove_taint tests
# ============================================================================


class TestRemoveTaint(unittest.TestCase):
    def _make_node_obj(self, taints=None, resource_version="123"):
        node = MagicMock()
        node.spec.taints = taints
        node.metadata.resourceVersion = resource_version
        return node

    def test_normal_remove(self):
        our_taint = make_taint(key="compactor-taint", value="true")
        other_taint = make_taint(key="other", value="val")
        node_obj = self._make_node_obj(taints=[our_taint, other_taint])
        client = MagicMock()
        client.get.return_value = node_obj

        remove_taint(client, "node-1", "compactor-taint", dry_run=False)

        client.get.assert_called_once_with(Node, "node-1")
        client.patch.assert_called_once()
        patch_arg = client.patch.call_args[0][2]
        self.assertEqual(patch_arg["metadata"]["resourceVersion"], "123")
        # Only the other taint should remain
        self.assertEqual(len(patch_arg["spec"]["taints"]), 1)
        self.assertEqual(patch_arg["spec"]["taints"][0].key, "other")

    def test_dry_run(self):
        client = MagicMock()
        remove_taint(client, "node-1", "compactor-taint", dry_run=True)
        client.get.assert_not_called()
        client.patch.assert_not_called()

    def test_all_taints_removed_sets_none(self):
        our_taint = make_taint(key="compactor-taint", value="true")
        node_obj = self._make_node_obj(taints=[our_taint])
        client = MagicMock()
        client.get.return_value = node_obj

        remove_taint(client, "node-1", "compactor-taint", dry_run=False)

        patch_arg = client.patch.call_args[0][2]
        self.assertIsNone(patch_arg["spec"]["taints"])

    def test_node_no_taints(self):
        """Node has no taints at all (spec.taints is None)."""
        node_obj = self._make_node_obj(taints=None)
        client = MagicMock()
        client.get.return_value = node_obj

        remove_taint(client, "node-1", "compactor-taint", dry_run=False)

        patch_arg = client.patch.call_args[0][2]
        self.assertIsNone(patch_arg["spec"]["taints"])

    def test_conflict_retry_succeeds(self):
        our_taint = make_taint(key="compactor-taint", value="true")
        node_obj = self._make_node_obj(taints=[our_taint])
        client = MagicMock()
        client.get.return_value = node_obj

        resp = MagicMock()
        err = ApiError(response=resp)
        err.status = MagicMock(code=409)

        # First patch fails with 409, second succeeds
        client.patch.side_effect = [err, None]

        remove_taint(client, "node-1", "compactor-taint", dry_run=False, max_retries=3)

        self.assertEqual(client.get.call_count, 2)
        self.assertEqual(client.patch.call_count, 2)

    def test_conflict_on_last_attempt_raises(self):
        our_taint = make_taint(key="compactor-taint", value="true")
        node_obj = self._make_node_obj(taints=[our_taint])
        client = MagicMock()
        client.get.return_value = node_obj

        resp = MagicMock()
        err = ApiError(response=resp)
        err.status = MagicMock(code=409)

        client.patch.side_effect = err

        with self.assertRaises(ApiError):
            remove_taint(client, "node-1", "compactor-taint", dry_run=False, max_retries=2)
        self.assertEqual(client.patch.call_count, 2)

    def test_non_409_error_raises_immediately(self):
        our_taint = make_taint(key="compactor-taint", value="true")
        node_obj = self._make_node_obj(taints=[our_taint])
        client = MagicMock()
        client.get.return_value = node_obj

        resp = MagicMock()
        err = ApiError(response=resp)
        err.status = MagicMock(code=500)

        client.patch.side_effect = err

        with self.assertRaises(ApiError):
            remove_taint(client, "node-1", "compactor-taint", dry_run=False, max_retries=3)
        # Should raise on first attempt, not retry
        self.assertEqual(client.patch.call_count, 1)

    def test_node_no_spec(self):
        """Node with spec=None should produce empty taints list."""
        node_obj = MagicMock()
        node_obj.spec = None
        node_obj.metadata.resourceVersion = "456"
        client = MagicMock()
        client.get.return_value = node_obj

        remove_taint(client, "node-1", "compactor-taint", dry_run=False)

        patch_arg = client.patch.call_args[0][2]
        # empty list is falsy -> None
        self.assertIsNone(patch_arg["spec"]["taints"])


# ============================================================================
# cleanup_stale_taints tests
# ============================================================================


class TestCleanupStaleTaints(unittest.TestCase):
    def test_removes_tainted_nodes(self):
        cfg = make_config()
        our_taint = make_taint(key=cfg.taint_key, value="true")
        node_with_taint = MagicMock()
        node_with_taint.spec.taints = [our_taint]
        node_with_taint.metadata.name = "node-1"

        # For the remove_taint call: the fresh get returns a node with our taint
        fresh_node = MagicMock()
        fresh_node.spec.taints = [our_taint]
        fresh_node.metadata.resourceVersion = "100"

        client = MagicMock()
        client.list.return_value = [node_with_taint]
        client.get.return_value = fresh_node

        cleanup_stale_taints(client, cfg)

        client.list.assert_called_once_with(Node)
        client.get.assert_called_once_with(Node, "node-1")
        client.patch.assert_called_once()

    def test_no_stale_taints(self):
        cfg = make_config()
        other_taint = make_taint(key="unrelated", value="true")
        node = MagicMock()
        node.spec.taints = [other_taint]
        node.metadata.name = "node-1"

        client = MagicMock()
        client.list.return_value = [node]

        cleanup_stale_taints(client, cfg)

        client.get.assert_not_called()
        client.patch.assert_not_called()

    def test_node_without_spec(self):
        cfg = make_config()
        node = MagicMock()
        node.spec = None

        client = MagicMock()
        client.list.return_value = [node]

        cleanup_stale_taints(client, cfg)

        client.get.assert_not_called()

    def test_node_without_taints(self):
        cfg = make_config()
        node = MagicMock()
        node.spec.taints = None

        client = MagicMock()
        client.list.return_value = [node]

        cleanup_stale_taints(client, cfg)

        client.get.assert_not_called()

    def test_multiple_nodes_some_tainted(self):
        cfg = make_config()
        our_taint = make_taint(key=cfg.taint_key, value="true")

        tainted_node = MagicMock()
        tainted_node.spec.taints = [our_taint]
        tainted_node.metadata.name = "tainted-1"

        clean_node = MagicMock()
        clean_node.spec.taints = [make_taint(key="other")]
        clean_node.metadata.name = "clean-1"

        no_taint_node = MagicMock()
        no_taint_node.spec.taints = None

        fresh_node = MagicMock()
        fresh_node.spec.taints = [our_taint]
        fresh_node.metadata.resourceVersion = "200"

        client = MagicMock()
        client.list.return_value = [tainted_node, clean_node, no_taint_node]
        client.get.return_value = fresh_node

        cleanup_stale_taints(client, cfg)

        # Only the tainted node should trigger remove_taint
        client.get.assert_called_once_with(Node, "tainted-1")
        client.patch.assert_called_once()


if __name__ == "__main__":
    unittest.main()
