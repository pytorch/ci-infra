"""Smoke tests for the NUMA-aware secondary scheduler.

Validates that the numa-scheduler namespace exists, the Helm release is
deployed, the scheduler Deployment is ready, and pods are running on
base-infrastructure nodes.
"""

from __future__ import annotations

import pytest
from helpers import (
    assert_deployment_ready,
    filter_pods,
    find_helm_release,
)

pytestmark = [pytest.mark.live]

NAMESPACE = "numa-scheduler"


# ============================================================================
# Namespace
# ============================================================================


class TestNUMASchedulerNamespace:
    """Verify numa-scheduler namespace exists."""

    def test_namespace_exists(self, all_namespaces: dict) -> None:
        ns_names = [ns["metadata"]["name"] for ns in all_namespaces.get("items", [])]
        assert NAMESPACE in ns_names, f"Namespace '{NAMESPACE}' not found"


# ============================================================================
# Helm Release
# ============================================================================


class TestNUMASchedulerHelm:
    """Verify numa-scheduler Helm release is deployed."""

    def test_helm_release_deployed(self, all_helm_releases: list[dict]) -> None:
        release = find_helm_release(all_helm_releases, "numa-scheduler", namespace=NAMESPACE)
        assert release is not None, "Helm release 'numa-scheduler' not found in 'numa-scheduler' namespace"
        status = release.get("status", "")
        assert status == "deployed", f"numa-scheduler Helm release status is '{status}', expected 'deployed'"


# ============================================================================
# Scheduler Deployment
# ============================================================================


class TestNUMASchedulerDeployment:
    """Verify the numa-scheduler Deployment is ready."""

    def test_deployment_ready(self, all_deployments: dict) -> None:
        assert_deployment_ready(all_deployments, NAMESPACE, "numa-scheduler")


# ============================================================================
# Scheduler Pods
# ============================================================================


class TestNUMASchedulerPods:
    """Verify scheduler pods are running on base-infrastructure nodes."""

    def test_pods_running(self, all_pods: dict) -> None:
        pods = filter_pods(all_pods, namespace=NAMESPACE)
        running = [p for p in pods if p.get("status", {}).get("phase") == "Running"]
        assert len(running) >= 1, f"Expected at least 1 Running numa-scheduler pod, found {len(running)}"

    def test_pods_on_base_infrastructure_nodes(self, all_pods: dict, all_nodes: dict) -> None:
        """Verify scheduler pods run on base-infrastructure nodes.

        The numa-scheduler values.yaml sets nodeSelector: role: base-infrastructure
        to colocate with other control plane components. This test catches
        misconfigurations that would schedule the secondary scheduler onto
        GPU worker nodes.
        """
        pods = filter_pods(all_pods, namespace=NAMESPACE)
        running = [p for p in pods if p.get("status", {}).get("phase") == "Running"]
        if not running:
            pytest.skip("No running numa-scheduler pods to verify node placement")

        node_labels: dict[str, dict] = {}
        for node in all_nodes.get("items", []):
            name = node.get("metadata", {}).get("name", "")
            labels = node.get("metadata", {}).get("labels", {})
            node_labels[name] = labels

        for pod in running:
            node_name = pod.get("spec", {}).get("nodeName", "")
            labels = node_labels.get(node_name, {})
            role = labels.get("role", "")
            assert role == "base-infrastructure", (
                f"Pod '{pod['metadata']['name']}' is on node '{node_name}' "
                f"with role='{role}', expected 'base-infrastructure'"
            )
