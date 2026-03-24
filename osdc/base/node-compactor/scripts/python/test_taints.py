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
        "fleet_cooldown": 900,
        "taint_rate": 0.3,
        "spare_capacity_nodes": 3,
        "spare_capacity_ratio": 0.15,
        "spare_capacity_threshold": 0.4,
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


def make_match_expression(key, operator, values=None):
    expr = MagicMock()
    expr.key = key
    expr.operator = operator
    expr.values = values or []
    return expr


def make_node_selector_term(match_expressions):
    term = MagicMock()
    term.matchExpressions = match_expressions
    return term


def make_pod(tolerations=None, cpu="1", memory="1Gi", has_spec=True, node_selector=None, affinity=None):
    pod = MagicMock()
    if not has_spec:
        pod.spec = None
        return pod
    pod.spec.tolerations = tolerations
    pod.spec.nodeSelector = node_selector
    pod.spec.affinity = affinity
    container = MagicMock()
    container.resources.requests = {"cpu": cpu, "memory": memory}
    pod.spec.containers = [container]
    return pod


def make_node_affinity_required(terms):
    """Build a pod affinity object with requiredDuringSchedulingIgnoredDuringExecution."""
    affinity = MagicMock()
    affinity.nodeAffinity.requiredDuringSchedulingIgnoredDuringExecution.nodeSelectorTerms = terms
    return affinity


def make_node_state(
    name="node-1",
    nodepool="pool-1",
    is_tainted=False,
    node_taints=None,
    allocatable_cpu=8.0,
    allocatable_memory=32 * GiB,
    pods=None,
    labels=None,
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
        labels=labels or {},
    )


# ============================================================================
# _pod_matches_node tests — toleration checks (existing, updated signature)
# ============================================================================


class TestPodMatchesNode(unittest.TestCase):
    def setUp(self):
        self.node = make_node_state()

    def test_no_taints_matches_all(self):
        pod = make_pod()
        self.assertTrue(_pod_matches_node(pod, self.node))

    def test_no_tolerations_with_taints_fails(self):
        pod = make_pod(tolerations=None)
        node = make_node_state(node_taints=[make_taint()])
        self.assertFalse(_pod_matches_node(pod, node))

    def test_pod_no_spec_with_taints_fails(self):
        pod = make_pod(has_spec=False)
        node = make_node_state(node_taints=[make_taint()])
        self.assertFalse(_pod_matches_node(pod, node))

    def test_matching_key_value_toleration(self):
        taint = make_taint(key="k1", value="v1", effect="NoSchedule")
        tol = make_toleration(key="k1", operator="Equal", value="v1", effect="NoSchedule")
        pod = make_pod(tolerations=[tol])
        node = make_node_state(node_taints=[taint])
        self.assertTrue(_pod_matches_node(pod, node))

    def test_exists_operator_no_key_wildcard(self):
        taint = make_taint(key="anything", value="val", effect="NoSchedule")
        tol = make_toleration(key=None, operator="Exists")
        pod = make_pod(tolerations=[tol])
        node = make_node_state(node_taints=[taint])
        self.assertTrue(_pod_matches_node(pod, node))

    def test_exists_operator_specific_key(self):
        taint = make_taint(key="k1", value="v1", effect="NoSchedule")
        tol = make_toleration(key="k1", operator="Exists")
        pod = make_pod(tolerations=[tol])
        node = make_node_state(node_taints=[taint])
        self.assertTrue(_pod_matches_node(pod, node))

    def test_effect_mismatch_continues(self):
        """Toleration effect doesn't match taint effect; should continue to next toleration."""
        taint = make_taint(key="k1", value="v1", effect="NoSchedule")
        tol_wrong_effect = make_toleration(key="k1", operator="Equal", value="v1", effect="NoExecute")
        tol_correct = make_toleration(key="k1", operator="Equal", value="v1", effect="NoSchedule")
        pod = make_pod(tolerations=[tol_wrong_effect, tol_correct])
        node = make_node_state(node_taints=[taint])
        self.assertTrue(_pod_matches_node(pod, node))

    def test_effect_mismatch_only_toleration_fails(self):
        taint = make_taint(key="k1", value="v1", effect="NoSchedule")
        tol = make_toleration(key="k1", operator="Equal", value="v1", effect="NoExecute")
        pod = make_pod(tolerations=[tol])
        node = make_node_state(node_taints=[taint])
        self.assertFalse(_pod_matches_node(pod, node))

    def test_value_mismatch(self):
        taint = make_taint(key="k1", value="v1", effect="NoSchedule")
        tol = make_toleration(key="k1", operator="Equal", value="wrong", effect="NoSchedule")
        pod = make_pod(tolerations=[tol])
        node = make_node_state(node_taints=[taint])
        self.assertFalse(_pod_matches_node(pod, node))

    def test_toleration_no_effect_matches_any_effect(self):
        """Toleration with no effect (None/empty) matches taint regardless of effect."""
        taint = make_taint(key="k1", value="v1", effect="NoSchedule")
        tol = make_toleration(key="k1", operator="Equal", value="v1", effect=None)
        pod = make_pod(tolerations=[tol])
        node = make_node_state(node_taints=[taint])
        self.assertTrue(_pod_matches_node(pod, node))

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
        node = make_node_state(node_taints=taints)
        self.assertTrue(_pod_matches_node(pod, node))

    def test_multiple_taints_one_not_tolerated(self):
        taints = [
            make_taint(key="k1", value="v1"),
            make_taint(key="k2", value="v2"),
        ]
        tols = [
            make_toleration(key="k1", operator="Equal", value="v1", effect="NoSchedule"),
        ]
        pod = make_pod(tolerations=tols)
        node = make_node_state(node_taints=taints)
        self.assertFalse(_pod_matches_node(pod, node))


# ============================================================================
# _pod_matches_node tests — nodeSelector checks
# ============================================================================


class TestPodMatchesNodeSelector(unittest.TestCase):
    def test_node_selector_match(self):
        pod = make_pod(node_selector={"tier": "gpu", "zone": "us-east-1a"})
        node = make_node_state(labels={"tier": "gpu", "zone": "us-east-1a", "extra": "val"})
        self.assertTrue(_pod_matches_node(pod, node))

    def test_node_selector_mismatch_value(self):
        pod = make_pod(node_selector={"tier": "gpu"})
        node = make_node_state(labels={"tier": "cpu"})
        self.assertFalse(_pod_matches_node(pod, node))

    def test_node_selector_missing_label(self):
        pod = make_pod(node_selector={"tier": "gpu"})
        node = make_node_state(labels={})
        self.assertFalse(_pod_matches_node(pod, node))

    def test_no_node_selector_matches(self):
        """Pod without nodeSelector should match any node."""
        pod = make_pod(node_selector=None)
        node = make_node_state(labels={"anything": "value"})
        self.assertTrue(_pod_matches_node(pod, node))

    def test_empty_node_selector_matches(self):
        """Pod with empty nodeSelector should match any node."""
        pod = make_pod(node_selector={})
        node = make_node_state(labels={"anything": "value"})
        self.assertTrue(_pod_matches_node(pod, node))


# ============================================================================
# _pod_matches_node tests — node affinity checks
# ============================================================================


class TestPodMatchesNodeAffinity(unittest.TestCase):
    def test_affinity_in_operator_match(self):
        expr = make_match_expression("gpu-type", "In", ["a100", "h100"])
        term = make_node_selector_term([expr])
        affinity = make_node_affinity_required([term])
        pod = make_pod(affinity=affinity)
        node = make_node_state(labels={"gpu-type": "a100"})
        self.assertTrue(_pod_matches_node(pod, node))

    def test_affinity_in_operator_no_match(self):
        expr = make_match_expression("gpu-type", "In", ["a100", "h100"])
        term = make_node_selector_term([expr])
        affinity = make_node_affinity_required([term])
        pod = make_pod(affinity=affinity)
        node = make_node_state(labels={"gpu-type": "v100"})
        self.assertFalse(_pod_matches_node(pod, node))

    def test_affinity_not_in_operator_match(self):
        expr = make_match_expression("gpu-type", "NotIn", ["v100"])
        term = make_node_selector_term([expr])
        affinity = make_node_affinity_required([term])
        pod = make_pod(affinity=affinity)
        node = make_node_state(labels={"gpu-type": "a100"})
        self.assertTrue(_pod_matches_node(pod, node))

    def test_affinity_not_in_operator_no_match(self):
        expr = make_match_expression("gpu-type", "NotIn", ["a100"])
        term = make_node_selector_term([expr])
        affinity = make_node_affinity_required([term])
        pod = make_pod(affinity=affinity)
        node = make_node_state(labels={"gpu-type": "a100"})
        self.assertFalse(_pod_matches_node(pod, node))

    def test_affinity_not_in_missing_label_matches(self):
        """NotIn with missing label should match (label not present = not in the set)."""
        expr = make_match_expression("gpu-type", "NotIn", ["a100"])
        term = make_node_selector_term([expr])
        affinity = make_node_affinity_required([term])
        pod = make_pod(affinity=affinity)
        node = make_node_state(labels={})
        self.assertTrue(_pod_matches_node(pod, node))

    def test_affinity_exists_operator_match(self):
        expr = make_match_expression("gpu", "Exists")
        term = make_node_selector_term([expr])
        affinity = make_node_affinity_required([term])
        pod = make_pod(affinity=affinity)
        node = make_node_state(labels={"gpu": "true"})
        self.assertTrue(_pod_matches_node(pod, node))

    def test_affinity_exists_operator_no_match(self):
        expr = make_match_expression("gpu", "Exists")
        term = make_node_selector_term([expr])
        affinity = make_node_affinity_required([term])
        pod = make_pod(affinity=affinity)
        node = make_node_state(labels={})
        self.assertFalse(_pod_matches_node(pod, node))

    def test_affinity_does_not_exist_match(self):
        expr = make_match_expression("spot", "DoesNotExist")
        term = make_node_selector_term([expr])
        affinity = make_node_affinity_required([term])
        pod = make_pod(affinity=affinity)
        node = make_node_state(labels={"gpu": "true"})
        self.assertTrue(_pod_matches_node(pod, node))

    def test_affinity_does_not_exist_no_match(self):
        expr = make_match_expression("spot", "DoesNotExist")
        term = make_node_selector_term([expr])
        affinity = make_node_affinity_required([term])
        pod = make_pod(affinity=affinity)
        node = make_node_state(labels={"spot": "true"})
        self.assertFalse(_pod_matches_node(pod, node))

    def test_affinity_gt_operator_match(self):
        expr = make_match_expression("gpu-count", "Gt", ["3"])
        term = make_node_selector_term([expr])
        affinity = make_node_affinity_required([term])
        pod = make_pod(affinity=affinity)
        node = make_node_state(labels={"gpu-count": "8"})
        self.assertTrue(_pod_matches_node(pod, node))

    def test_affinity_gt_operator_no_match_equal(self):
        expr = make_match_expression("gpu-count", "Gt", ["8"])
        term = make_node_selector_term([expr])
        affinity = make_node_affinity_required([term])
        pod = make_pod(affinity=affinity)
        node = make_node_state(labels={"gpu-count": "8"})
        self.assertFalse(_pod_matches_node(pod, node))

    def test_affinity_gt_operator_missing_label(self):
        expr = make_match_expression("gpu-count", "Gt", ["3"])
        term = make_node_selector_term([expr])
        affinity = make_node_affinity_required([term])
        pod = make_pod(affinity=affinity)
        node = make_node_state(labels={})
        self.assertFalse(_pod_matches_node(pod, node))

    def test_affinity_lt_operator_match(self):
        expr = make_match_expression("gpu-count", "Lt", ["8"])
        term = make_node_selector_term([expr])
        affinity = make_node_affinity_required([term])
        pod = make_pod(affinity=affinity)
        node = make_node_state(labels={"gpu-count": "4"})
        self.assertTrue(_pod_matches_node(pod, node))

    def test_affinity_lt_operator_no_match_equal(self):
        expr = make_match_expression("gpu-count", "Lt", ["4"])
        term = make_node_selector_term([expr])
        affinity = make_node_affinity_required([term])
        pod = make_pod(affinity=affinity)
        node = make_node_state(labels={"gpu-count": "4"})
        self.assertFalse(_pod_matches_node(pod, node))

    def test_multiple_terms_or_logic_first_matches(self):
        """Multiple nodeSelectorTerms are OR'd — first term matches."""
        expr1 = make_match_expression("tier", "In", ["gpu"])
        expr2 = make_match_expression("tier", "In", ["cpu"])
        term1 = make_node_selector_term([expr1])
        term2 = make_node_selector_term([expr2])
        affinity = make_node_affinity_required([term1, term2])
        pod = make_pod(affinity=affinity)
        node = make_node_state(labels={"tier": "gpu"})
        self.assertTrue(_pod_matches_node(pod, node))

    def test_multiple_terms_or_logic_second_matches(self):
        """Multiple nodeSelectorTerms are OR'd — second term matches."""
        expr1 = make_match_expression("tier", "In", ["gpu"])
        expr2 = make_match_expression("tier", "In", ["cpu"])
        term1 = make_node_selector_term([expr1])
        term2 = make_node_selector_term([expr2])
        affinity = make_node_affinity_required([term1, term2])
        pod = make_pod(affinity=affinity)
        node = make_node_state(labels={"tier": "cpu"})
        self.assertTrue(_pod_matches_node(pod, node))

    def test_multiple_terms_or_logic_none_match(self):
        """Multiple nodeSelectorTerms are OR'd — none match."""
        expr1 = make_match_expression("tier", "In", ["gpu"])
        expr2 = make_match_expression("tier", "In", ["cpu"])
        term1 = make_node_selector_term([expr1])
        term2 = make_node_selector_term([expr2])
        affinity = make_node_affinity_required([term1, term2])
        pod = make_pod(affinity=affinity)
        node = make_node_state(labels={"tier": "storage"})
        self.assertFalse(_pod_matches_node(pod, node))

    def test_multiple_expressions_and_logic_all_match(self):
        """Multiple matchExpressions in a term are AND'd — all match."""
        expr1 = make_match_expression("tier", "In", ["gpu"])
        expr2 = make_match_expression("zone", "In", ["us-east-1a"])
        term = make_node_selector_term([expr1, expr2])
        affinity = make_node_affinity_required([term])
        pod = make_pod(affinity=affinity)
        node = make_node_state(labels={"tier": "gpu", "zone": "us-east-1a"})
        self.assertTrue(_pod_matches_node(pod, node))

    def test_multiple_expressions_and_logic_one_fails(self):
        """Multiple matchExpressions in a term are AND'd — one fails."""
        expr1 = make_match_expression("tier", "In", ["gpu"])
        expr2 = make_match_expression("zone", "In", ["us-west-2a"])
        term = make_node_selector_term([expr1, expr2])
        affinity = make_node_affinity_required([term])
        pod = make_pod(affinity=affinity)
        node = make_node_state(labels={"tier": "gpu", "zone": "us-east-1a"})
        self.assertFalse(_pod_matches_node(pod, node))

    def test_no_affinity_matches(self):
        """Pod without affinity matches any node."""
        pod = make_pod(affinity=None)
        node = make_node_state(labels={"anything": "value"})
        self.assertTrue(_pod_matches_node(pod, node))


# ============================================================================
# _pod_matches_node tests — resource fit checks
# ============================================================================


class TestPodMatchesNodeResources(unittest.TestCase):
    def test_pod_fits_in_remaining_capacity(self):
        """Pod fits within remaining CPU and memory."""
        # 8 CPU, 32 GiB allocatable; 2 CPU, 4 GiB used -> 6 CPU, 28 GiB remaining
        node = make_node_state(
            allocatable_cpu=8.0,
            allocatable_memory=32 * GiB,
            pods=[PodInfo("p1", "ns", 2.0, 4 * GiB, "node-1", False, NOW)],
        )
        pod = make_pod(cpu="4", memory="16Gi")
        self.assertTrue(_pod_matches_node(pod, node))

    def test_pod_exceeds_cpu_capacity(self):
        """Pod doesn't fit — not enough CPU."""
        node = make_node_state(
            allocatable_cpu=8.0,
            allocatable_memory=32 * GiB,
            pods=[PodInfo("p1", "ns", 7.0, 1 * GiB, "node-1", False, NOW)],
        )
        pod = make_pod(cpu="2", memory="1Gi")
        self.assertFalse(_pod_matches_node(pod, node))

    def test_pod_exceeds_memory_capacity(self):
        """Pod doesn't fit — not enough memory."""
        node = make_node_state(
            allocatable_cpu=8.0,
            allocatable_memory=32 * GiB,
            pods=[PodInfo("p1", "ns", 1.0, 30 * GiB, "node-1", False, NOW)],
        )
        pod = make_pod(cpu="1", memory="4Gi")
        self.assertFalse(_pod_matches_node(pod, node))

    def test_daemonset_pods_count_toward_used(self):
        """DaemonSet pod CPU/memory counts toward total_used for resource fit."""
        node = make_node_state(
            allocatable_cpu=4.0,
            allocatable_memory=8 * GiB,
            pods=[PodInfo("ds", "ns", 3.0, 6 * GiB, "node-1", True, NOW)],
        )
        pod = make_pod(cpu="2", memory="1Gi")
        self.assertFalse(_pod_matches_node(pod, node))

    def test_empty_node_fits_any_pod(self):
        """Empty node has full capacity available."""
        node = make_node_state(allocatable_cpu=8.0, allocatable_memory=32 * GiB, pods=[])
        pod = make_pod(cpu="8", memory="32Gi")
        self.assertTrue(_pod_matches_node(pod, node))


# ============================================================================
# _pod_matches_node tests — combined constraint checks
# ============================================================================


class TestPodMatchesNodeCombined(unittest.TestCase):
    def test_tolerations_pass_but_selector_fails(self):
        """Pod tolerates taints but nodeSelector doesn't match."""
        taint = make_taint(key="gpu", value="true", effect="NoSchedule")
        tol = make_toleration(key="gpu", operator="Equal", value="true", effect="NoSchedule")
        pod = make_pod(tolerations=[tol], node_selector={"tier": "gpu"})
        node = make_node_state(node_taints=[taint], labels={"tier": "cpu"})
        self.assertFalse(_pod_matches_node(pod, node))

    def test_selector_passes_but_tolerations_fail(self):
        """nodeSelector matches but pod can't tolerate taints."""
        taint = make_taint(key="gpu", value="true", effect="NoSchedule")
        pod = make_pod(tolerations=None, node_selector={"tier": "gpu"})
        node = make_node_state(node_taints=[taint], labels={"tier": "gpu"})
        self.assertFalse(_pod_matches_node(pod, node))

    def test_all_constraints_pass(self):
        """Pod passes all: tolerations, nodeSelector, affinity, resource fit."""
        taint = make_taint(key="gpu", value="true", effect="NoSchedule")
        tol = make_toleration(key="gpu", operator="Equal", value="true", effect="NoSchedule")
        expr = make_match_expression("zone", "In", ["us-east-1a"])
        term = make_node_selector_term([expr])
        affinity = make_node_affinity_required([term])
        pod = make_pod(
            tolerations=[tol],
            node_selector={"tier": "gpu"},
            affinity=affinity,
            cpu="2",
            memory="4Gi",
        )
        node = make_node_state(
            node_taints=[taint],
            labels={"tier": "gpu", "zone": "us-east-1a"},
            allocatable_cpu=8.0,
            allocatable_memory=32 * GiB,
        )
        self.assertTrue(_pod_matches_node(pod, node))

    def test_affinity_passes_but_resources_fail(self):
        """Pod passes affinity + tolerations + selector but doesn't fit."""
        taint = make_taint(key="gpu", value="true", effect="NoSchedule")
        tol = make_toleration(key="gpu", operator="Equal", value="true", effect="NoSchedule")
        pod = make_pod(
            tolerations=[tol],
            node_selector={"tier": "gpu"},
            cpu="16",
            memory="1Gi",
        )
        node = make_node_state(
            node_taints=[taint],
            labels={"tier": "gpu"},
            allocatable_cpu=8.0,
            allocatable_memory=32 * GiB,
        )
        self.assertFalse(_pod_matches_node(pod, node))


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

        def controlled_match(p, node_state):
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
