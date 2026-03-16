"""Smoke tests for BuildKit build service.

Validates that the BuildKit namespace, Deployments, Services, NetworkPolicies,
ConfigMap, and Karpenter NodePools are present and healthy.
"""

from __future__ import annotations

import pytest
from helpers import assert_deployment_ready, filter_services, run_kubectl

pytestmark = [pytest.mark.live]

NAMESPACE = "buildkit"


# ============================================================================
# Namespace
# ============================================================================


class TestBuildKitNamespace:
    """Verify the buildkit namespace exists."""

    def test_namespace_exists(self, all_namespaces: dict) -> None:
        ns_names = [ns["metadata"]["name"] for ns in all_namespaces.get("items", [])]
        assert NAMESPACE in ns_names, f"Namespace '{NAMESPACE}' not found. Available: {ns_names}"


# ============================================================================
# Deployments
# ============================================================================


class TestBuildKitDeployments:
    """Verify buildkitd Deployments are running with correct replica counts."""

    def test_buildkitd_arm64_ready(self, all_deployments: dict) -> None:
        """buildkitd-arm64 Deployment exists and has expected replicas."""
        assert_deployment_ready(all_deployments, NAMESPACE, "buildkitd-arm64")

    def test_buildkitd_amd64_ready(self, all_deployments: dict) -> None:
        """buildkitd-amd64 Deployment exists and has expected replicas."""
        assert_deployment_ready(all_deployments, NAMESPACE, "buildkitd-amd64")

    def test_buildkitd_lb_ready(self, all_deployments: dict) -> None:
        """HAProxy load balancer Deployment exists with ready replicas."""
        assert_deployment_ready(all_deployments, NAMESPACE, "buildkitd-lb")


# ============================================================================
# Services
# ============================================================================


class TestBuildKitServices:
    """Verify BuildKit Services exist."""

    @pytest.mark.parametrize("svc_name", ["buildkitd-arm64", "buildkitd-amd64", "buildkitd"])
    def test_service_exists(self, all_services: dict, svc_name: str) -> None:
        svcs = filter_services(all_services, namespace=NAMESPACE, name=svc_name)
        assert len(svcs) == 1, f"Expected Service '{svc_name}' in namespace '{NAMESPACE}'"


# ============================================================================
# NetworkPolicies
# ============================================================================


class TestBuildKitNetworkPolicies:
    """Verify at least one NetworkPolicy exists in the buildkit namespace."""

    def test_networkpolicies_exist(self) -> None:
        result = run_kubectl(["get", "networkpolicies", "-o", "json"], namespace=NAMESPACE)
        items = result.get("items", [])
        assert len(items) >= 1, "Expected at least 1 NetworkPolicy in buildkit namespace"


# ============================================================================
# ConfigMap
# ============================================================================


class TestBuildKitConfig:
    """Verify the buildkitd configuration ConfigMap exists."""

    def test_config_exists(self) -> None:
        result = run_kubectl(["get", "configmap", "buildkitd-config", "-o", "json"], namespace=NAMESPACE)
        name = result.get("metadata", {}).get("name", "")
        assert name == "buildkitd-config", "ConfigMap 'buildkitd-config' not found in buildkit namespace"


# ============================================================================
# Karpenter NodePools
# ============================================================================


class TestBuildKitNodePools:
    """Verify Karpenter NodePools for BuildKit exist."""

    def test_nodepools_exist(self, all_nodepools: dict) -> None:
        """At least one NodePool related to BuildKit exists."""
        buildkit_pools = [
            np
            for np in all_nodepools.get("items", [])
            if "buildkit" in np.get("metadata", {}).get("name", "")
            or np.get("metadata", {}).get("labels", {}).get("osdc.io/module") == "buildkit"
        ]
        assert len(buildkit_pools) >= 1, "Expected at least 1 Karpenter NodePool for BuildKit"
