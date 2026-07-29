"""Smoke tests for the ARC capacity-monitor RBAC.

The capacity monitor runs inside each AutoscalingListener pod (namespace
arc-systems, one dynamically-created ServiceAccount per scale set, all covered
by the group system:serviceaccounts:arc-systems). It requires namespaced
pod/configmap management in arc-runners to create and reap placeholder pods,
and cluster-scoped node read for the schedulability check that drops
placeholders on cordoned or disruption-tainted nodes. A missing grant degrades
capacity accounting silently instead of crashing, so these tests fail loudly if
any of the four RBAC objects is dropped.
"""

from __future__ import annotations

import pytest
from helpers import run_kubectl

pytestmark = [pytest.mark.live]

NS_RUNNERS = "arc-runners"
SA_GROUP = "system:serviceaccounts:arc-systems"

ROLE_NAME = "arc-capacity-monitor"
NODES_CLUSTERROLE_NAME = "arc-capacity-monitor-nodes"

NODE_VERBS = {"get", "list", "watch"}
NAMESPACED_VERBS = {"create", "delete", "deletecollection", "get", "list", "watch"}


def _verbs_for_resource(rules: list[dict], resource: str) -> set[str]:
    return {verb for rule in rules if resource in rule.get("resources", []) for verb in rule.get("verbs", [])}


def _binds_group(binding: dict, group: str) -> bool:
    return any(
        subject.get("kind") == "Group" and subject.get("name") == group for subject in binding.get("subjects", [])
    )


class TestCapacityMonitorNodesRBAC:
    """Cluster-scoped node read for the listener schedulability check."""

    def test_clusterrole_grants_node_read(self) -> None:
        role = run_kubectl(["get", "clusterrole", NODES_CLUSTERROLE_NAME])
        verbs = _verbs_for_resource(role.get("rules", []), "nodes")
        assert verbs >= NODE_VERBS, (
            f"ClusterRole {NODES_CLUSTERROLE_NAME} must grant {sorted(NODE_VERBS)} on "
            f"nodes for the listener node-schedulability check; got {sorted(verbs)}"
        )

    def test_clusterrolebinding_targets_listener_group(self) -> None:
        binding = run_kubectl(["get", "clusterrolebinding", NODES_CLUSTERROLE_NAME])
        role_ref = binding.get("roleRef", {})
        assert role_ref.get("name") == NODES_CLUSTERROLE_NAME, (
            f"ClusterRoleBinding {NODES_CLUSTERROLE_NAME} roleRef must point at "
            f"ClusterRole {NODES_CLUSTERROLE_NAME}; got {role_ref}"
        )
        assert role_ref.get("kind") == "ClusterRole", (
            f"ClusterRoleBinding {NODES_CLUSTERROLE_NAME} roleRef.kind must be ClusterRole; got {role_ref.get('kind')}"
        )
        assert _binds_group(binding, SA_GROUP), (
            f"ClusterRoleBinding {NODES_CLUSTERROLE_NAME} must bind group {SA_GROUP}; got {binding.get('subjects')}"
        )


class TestCapacityMonitorPlaceholderRBAC:
    """Namespaced pod/configmap management for placeholder pods in arc-runners."""

    def test_role_grants_pod_and_configmap_management(self) -> None:
        role = run_kubectl(["get", "role", ROLE_NAME], namespace=NS_RUNNERS)
        rules = role.get("rules", [])
        for resource in ("pods", "configmaps"):
            verbs = _verbs_for_resource(rules, resource)
            assert verbs >= NAMESPACED_VERBS, (
                f"Role {ROLE_NAME} in {NS_RUNNERS} must grant "
                f"{sorted(NAMESPACED_VERBS)} on {resource}; got {sorted(verbs)}"
            )

    def test_rolebinding_targets_listener_group(self) -> None:
        binding = run_kubectl(["get", "rolebinding", ROLE_NAME], namespace=NS_RUNNERS)
        role_ref = binding.get("roleRef", {})
        assert role_ref.get("name") == ROLE_NAME, (
            f"RoleBinding {ROLE_NAME} roleRef must point at Role {ROLE_NAME}; got {role_ref}"
        )
        assert role_ref.get("kind") == "Role", (
            f"RoleBinding {ROLE_NAME} roleRef.kind must be Role; got {role_ref.get('kind')}"
        )
        assert _binds_group(binding, SA_GROUP), (
            f"RoleBinding {ROLE_NAME} in {NS_RUNNERS} must bind group {SA_GROUP}; got {binding.get('subjects')}"
        )
