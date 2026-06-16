"""Smoke tests for NFD (Node Feature Discovery) topology-updater.

Validates that the NFD namespace exists, the Helm release is deployed,
the topology-updater DaemonSet is healthy on all nodes, and
NodeResourceTopology CRDs are being published.
"""

from __future__ import annotations

import subprocess

import pytest
from helpers import (
    assert_daemonset_healthy,
    find_helm_release,
    run_kubectl,
)

pytestmark = [pytest.mark.live]

NAMESPACE = "nfd"


# ============================================================================
# Namespace
# ============================================================================


class TestNFDNamespace:
    """Verify NFD namespace exists."""

    def test_namespace_exists(self, all_namespaces: dict) -> None:
        ns_names = [ns["metadata"]["name"] for ns in all_namespaces.get("items", [])]
        assert NAMESPACE in ns_names, f"Namespace '{NAMESPACE}' not found"


# ============================================================================
# Helm Release
# ============================================================================


class TestNFDHelm:
    """Verify NFD Helm release is deployed."""

    def test_helm_release_deployed(self, all_helm_releases: list[dict]) -> None:
        release = find_helm_release(all_helm_releases, "nfd", namespace=NAMESPACE)
        assert release is not None, "Helm release 'nfd' not found in 'nfd' namespace"
        status = release.get("status", "")
        assert status == "deployed", f"NFD Helm release status is '{status}', expected 'deployed'"


# ============================================================================
# Topology-updater DaemonSet
# ============================================================================


class TestTopologyUpdater:
    """Verify topology-updater DaemonSet is healthy on p5 nodes."""

    def test_topology_updater_healthy(self, all_daemonsets: dict, all_nodes: dict) -> None:
        assert_daemonset_healthy(
            all_daemonsets,
            all_nodes,
            NAMESPACE,
            name_contains="topology-updater",
            allow_zero=True,  # NFD targets p5 (H100) nodes only; clusters without p5 have 0 pods
        )


class TestTaintRemover:
    """Verify nfd-taint-remover DaemonSet is healthy on p5 nodes."""

    def test_taint_remover_healthy(self, all_daemonsets: dict, all_nodes: dict) -> None:
        assert_daemonset_healthy(
            all_daemonsets,
            all_nodes,
            NAMESPACE,
            name_contains="taint-remover",
            allow_zero=True,  # Same p5-only scope as topology-updater
        )


# ============================================================================
# NodeResourceTopology CRD
# ============================================================================


class TestNodeResourceTopology:
    """Verify NodeResourceTopology CRD is installed.

    The NRT CRD is the key output of NFD topology-updater — without it
    the numa-scheduler has no NUMA visibility. The CRD is installed by
    the NFD Helm chart when topologyUpdater is enabled.
    """

    def test_nrt_crd_exists(self) -> None:
        try:
            result = run_kubectl(
                ["get", "crd", "noderesourcetopologies.topology.node.k8s.io", "-o", "json"],
            )
        except subprocess.CalledProcessError:
            pytest.fail("NodeResourceTopology CRD not found — NFD chart may not have installed it")
        name = result.get("metadata", {}).get("name", "")
        assert "noderesourcetopologies" in name
