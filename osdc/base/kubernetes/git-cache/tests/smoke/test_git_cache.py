"""Smoke tests for the two-tier git clone cache."""

import pytest
from helpers import assert_daemonset_ready, assert_deployment_ready, filter_services, run_kubectl

pytestmark = [pytest.mark.live]

GIT_CACHE_NAMESPACE = "kube-system"


# ============================================================================
# Git Cache Central
# ============================================================================


class TestGitCacheCentral:
    """Verify the central git cache Deployment, Services, and PDB."""

    def test_deployment_exists_and_ready(self, all_deployments):
        assert_deployment_ready(all_deployments, GIT_CACHE_NAMESPACE, "git-cache-central")

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

    def test_daemonset_exists_and_ready(self, all_daemonsets):
        assert_daemonset_ready(all_daemonsets, GIT_CACHE_NAMESPACE, "git-cache-warmer")


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
