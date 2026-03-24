"""Smoke tests for the two-tier git clone cache."""

import pytest
from helpers import assert_daemonset_healthy, filter_services, run_kubectl

pytestmark = [pytest.mark.live]

GIT_CACHE_NAMESPACE = "kube-system"


# ============================================================================
# Git Cache Central
# ============================================================================


class TestGitCacheCentral:
    """Verify the central git cache StatefulSet, Services, and PDB."""

    def test_statefulset_exists_and_ready(self):
        """Verify StatefulSet git-cache-central exists with correct replica count."""
        result = run_kubectl(["get", "statefulset", "git-cache-central"], namespace=GIT_CACHE_NAMESPACE)
        assert result["metadata"]["name"] == "git-cache-central"
        spec_replicas = result["spec"]["replicas"]
        ready_replicas = result.get("status", {}).get("readyReplicas", 0)
        assert ready_replicas == spec_replicas, f"StatefulSet git-cache-central: {ready_replicas}/{spec_replicas} ready"

    def test_headless_service_exists(self, all_services):
        svcs = filter_services(all_services, namespace=GIT_CACHE_NAMESPACE, name="git-cache-central-headless")
        assert len(svcs) == 1, "Service git-cache-central-headless not found in kube-system"
        svc = svcs[0]
        assert svc["spec"].get("clusterIP") == "None", "Headless service should have clusterIP=None"
        assert svc["spec"].get("publishNotReadyAddresses") is True, (
            "Headless service must set publishNotReadyAddresses=true for rollout discovery"
        )
        ports = [p["port"] for p in svc.get("spec", {}).get("ports", [])]
        assert 873 in ports, f"Headless service missing rsync port 873. Ports: {ports}"
        assert 9101 in ports, f"Headless service missing metrics port 9101. Ports: {ports}"

    def test_rsync_service_exists(self, all_services):
        svcs = filter_services(all_services, namespace=GIT_CACHE_NAMESPACE, name="git-cache-central")
        assert len(svcs) == 1, "Service git-cache-central not found in kube-system"
        ports = [p["port"] for p in svcs[0].get("spec", {}).get("ports", [])]
        assert 873 in ports, f"Rsync service missing port 873. Ports: {ports}"

    def test_metrics_service_exists(self, all_services):
        svcs = filter_services(all_services, namespace=GIT_CACHE_NAMESPACE, name="git-cache-central-metrics")
        assert len(svcs) == 1, "Service git-cache-central-metrics not found in kube-system"
        ports = [p["port"] for p in svcs[0].get("spec", {}).get("ports", [])]
        assert 9101 in ports, f"Metrics service missing port 9101. Ports: {ports}"

    def test_pdb_exists(self):
        result = run_kubectl(["get", "pdb", "git-cache-central"], namespace=GIT_CACHE_NAMESPACE)
        assert result["metadata"]["name"] == "git-cache-central"


# ============================================================================
# Git Cache DaemonSet
# ============================================================================


class TestGitCacheDaemonSet:
    """Verify the git cache rsync DaemonSet."""

    def test_daemonset_exists_and_ready(self, all_daemonsets, all_nodes):
        assert_daemonset_healthy(
            all_daemonsets,
            all_nodes,
            GIT_CACHE_NAMESPACE,
            "git-cache-warmer",
            node_selector={"workload-type": ["github-runner", "buildkit"]},
        )


# ============================================================================
# Git Cache RBAC
# ============================================================================


class TestGitCacheRBAC:
    """Verify RBAC resources for the git cache."""

    @pytest.mark.parametrize("sa_name", ["git-cache-warmer"])
    def test_service_account_exists(self, sa_name):
        result = run_kubectl(["get", "serviceaccount", sa_name], namespace=GIT_CACHE_NAMESPACE)
        assert result["metadata"]["name"] == sa_name

    def test_cluster_role_exists(self):
        result = run_kubectl(["get", "clusterrole", "git-cache-warmer"])
        assert result["metadata"]["name"] == "git-cache-warmer"
