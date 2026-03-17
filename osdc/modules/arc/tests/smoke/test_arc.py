"""Smoke tests for ARC (Actions Runner Controller).

Validates that the ARC namespaces exist, shared runner resources are present,
the Helm release is deployed, and controller pods are running.
"""

from __future__ import annotations

import pytest
from helpers import filter_pods, find_helm_release, run_kubectl

pytestmark = [pytest.mark.live]

NS_SYSTEMS = "arc-systems"
NS_RUNNERS = "arc-runners"


# ============================================================================
# Namespaces
# ============================================================================


class TestARCNamespaces:
    """Verify ARC namespaces exist."""

    @pytest.mark.parametrize("namespace", [NS_SYSTEMS, NS_RUNNERS])
    def test_namespace_exists(self, all_namespaces: dict, namespace: str) -> None:
        ns_names = [ns["metadata"]["name"] for ns in all_namespaces.get("items", [])]
        assert namespace in ns_names, f"Namespace '{namespace}' not found"


# ============================================================================
# Shared runner resources
# ============================================================================


class TestARCResources:
    """Verify shared resources in the arc-runners namespace."""

    def test_runner_serviceaccount(self) -> None:
        result = run_kubectl(["get", "serviceaccount", "arc-runner"], namespace=NS_RUNNERS)
        assert result["metadata"]["name"] == "arc-runner"

    def test_runner_limitrange(self) -> None:
        result = run_kubectl(["get", "limitrange", "guaranteed-qos-defaults"], namespace=NS_RUNNERS)
        assert result["metadata"]["name"] == "guaranteed-qos-defaults"

    def test_runner_hooks_configmap(self) -> None:
        result = run_kubectl(["get", "configmap", "runner-hooks"], namespace=NS_RUNNERS)
        assert result["metadata"]["name"] == "runner-hooks"


# ============================================================================
# Helm release
# ============================================================================


class TestARCHelm:
    """Verify the ARC Helm release is deployed."""

    def test_helm_release_deployed(self, all_helm_releases: list[dict]) -> None:
        release = find_helm_release(all_helm_releases, "arc", namespace=NS_SYSTEMS)
        assert release is not None, "Helm release 'arc' not found in 'arc-systems' namespace"
        assert release["status"] == "deployed", (
            f"Helm release 'arc' status is '{release['status']}', expected 'deployed'"
        )


# ============================================================================
# Controller pods
# ============================================================================


class TestARCController:
    """Verify ARC controller pods are running."""

    def test_controller_pods_running(self, all_pods: dict) -> None:
        pods = filter_pods(all_pods, namespace=NS_SYSTEMS, labels={"app.kubernetes.io/name": "gha-rs-controller"})
        running = [p for p in pods if p.get("status", {}).get("phase") == "Running"]
        assert len(running) >= 1, f"Expected at least 1 Running ARC controller pod, found {len(running)}"
