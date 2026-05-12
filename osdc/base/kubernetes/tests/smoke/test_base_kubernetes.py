"""Smoke tests for base Kubernetes resources (DaemonSets, StorageClass, nodes)."""

import pytest
from helpers import assert_daemonset_healthy, filter_services

pytestmark = [pytest.mark.live]

NAMESPACE = "kube-system"


# ============================================================================
# Base DaemonSets
# ============================================================================


class TestBaseDaemonSets:
    """Verify base DaemonSets are running on all expected nodes."""

    def test_nvidia_device_plugin(self, all_daemonsets, all_nodes):
        # 0/0 is OK if no GPU nodes are present
        assert_daemonset_healthy(
            all_daemonsets, all_nodes, NAMESPACE, "nvidia-device-plugin-daemonset", allow_zero=True
        )

    def test_registry_mirror_config(self, all_daemonsets, all_nodes):
        assert_daemonset_healthy(all_daemonsets, all_nodes, NAMESPACE, "registry-mirror-config")

    def test_node_performance_tuning(self, all_daemonsets, all_nodes):
        assert_daemonset_healthy(all_daemonsets, all_nodes, NAMESPACE, "node-performance-tuning", allow_zero=True)

    def test_nodelocaldns_daemonset(self, all_daemonsets, all_nodes):
        """NodeLocal DNSCache DaemonSet runs on every node."""
        assert_daemonset_healthy(all_daemonsets, all_nodes, NAMESPACE, "node-local-dns")


# ============================================================================
# NodeLocal DNSCache Services
# ============================================================================


class TestNodeLocalDNSServices:
    """Verify NodeLocal DNSCache supporting Services are present and correctly configured."""

    def test_nodelocaldns_metrics_service_headless(self, all_services):
        """NLD metrics service is headless (clusterIP: None) for PodMonitor discovery."""
        svcs = filter_services(all_services, namespace=NAMESPACE, name="node-local-dns-metrics")
        assert len(svcs) == 1, f"node-local-dns-metrics service not found in {NAMESPACE}"
        cluster_ip = svcs[0]["spec"].get("clusterIP")
        assert cluster_ip == "None", f"expected headless service (clusterIP: None), got {cluster_ip!r}"

    def test_nodelocaldns_upstream_service_selects_kube_dns(self, all_services):
        """kube-dns-upstream service exists and selects kube-dns pods (NLD's forward target)."""
        svcs = filter_services(all_services, namespace=NAMESPACE, name="kube-dns-upstream")
        assert len(svcs) == 1, f"kube-dns-upstream service not found in {NAMESPACE}"
        selector = svcs[0]["spec"].get("selector", {})
        assert selector.get("k8s-app") == "kube-dns", f"expected selector k8s-app=kube-dns, got {selector!r}"


# ============================================================================
# Base Nodes
# ============================================================================


class TestBaseNodes:
    """Verify base infrastructure nodes are present and correctly configured."""

    def test_base_node_count(self, all_nodes, resolve_config):
        expected_count = int(resolve_config("base.base_node_count", "3"))
        base_nodes = [
            n
            for n in all_nodes["items"]
            if n.get("metadata", {}).get("labels", {}).get("role") == "base-infrastructure"
        ]
        assert len(base_nodes) >= expected_count, f"Expected >= {expected_count} base nodes, found {len(base_nodes)}"

    def test_base_nodes_ready(self, all_nodes):
        base_nodes = [
            n
            for n in all_nodes["items"]
            if n.get("metadata", {}).get("labels", {}).get("role") == "base-infrastructure"
        ]
        for node in base_nodes:
            name = node["metadata"]["name"]
            conditions = {c["type"]: c["status"] for c in node.get("status", {}).get("conditions", [])}
            assert conditions.get("Ready") == "True", f"Base node {name} is not Ready"

    def test_base_nodes_have_critical_addons_taint(self, all_nodes):
        base_nodes = [
            n
            for n in all_nodes["items"]
            if n.get("metadata", {}).get("labels", {}).get("role") == "base-infrastructure"
        ]
        assert len(base_nodes) > 0, "No base nodes found"
        for node in base_nodes:
            name = node["metadata"]["name"]
            taints = node.get("spec", {}).get("taints", [])
            has_taint = any(t.get("key") == "CriticalAddonsOnly" and t.get("effect") == "NoSchedule" for t in taints)
            assert has_taint, f"Base node {name} missing CriticalAddonsOnly taint"


# ============================================================================
# StorageClass
# ============================================================================


class TestStorageClass:
    """Verify the gp3 StorageClass is configured as default."""

    def test_gp3_storageclass_exists(self, all_storageclasses):
        sc_names = [sc["metadata"]["name"] for sc in all_storageclasses.get("items", [])]
        assert "gp3" in sc_names, f"StorageClass 'gp3' not found. Available: {sc_names}"

    def test_gp3_is_default(self, all_storageclasses):
        for sc in all_storageclasses.get("items", []):
            if sc["metadata"]["name"] == "gp3":
                annotations = sc.get("metadata", {}).get("annotations", {})
                is_default = annotations.get("storageclass.kubernetes.io/is-default-class", "false")
                assert is_default == "true", "StorageClass 'gp3' is not the default"
                return
        pytest.fail("StorageClass 'gp3' not found")
