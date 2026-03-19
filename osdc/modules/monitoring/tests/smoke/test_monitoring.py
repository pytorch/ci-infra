"""Smoke tests for the monitoring stack.

Validates that kube-prometheus-stack, node-exporter, kube-state-metrics,
ServiceMonitors, PodMonitors, DCGM exporter, and optionally Alloy are
deployed and healthy.
"""

from __future__ import annotations

import pytest
from helpers import (
    assert_daemonset_healthy,
    assert_deployment_ready,
    fetch_grafana_cloud_credentials,
    filter_deployments,
    find_helm_release,
    mimir_read_url,
    query_mimir,
    run_kubectl,
)

pytestmark = [pytest.mark.live]

EXPECTED_SERVICE_MONITORS = [
    "arc-controller",
    "harbor",
    "karpenter",
    "node-compactor",
    "git-cache-central",
    "dcgm-exporter",
]

EXPECTED_POD_MONITORS = [
    "arc-listeners",
    "git-cache-daemonset",
]


@pytest.fixture
def mon_ns(resolve_config) -> str:
    """Resolve the monitoring namespace from cluster config."""
    return resolve_config("monitoring.namespace", "monitoring")


# ============================================================================
# Helm Release
# ============================================================================


class TestMonitoringHelm:
    """Verify kube-prometheus-stack Helm release is deployed."""

    def test_helm_release_deployed(self, all_helm_releases: list[dict], mon_ns: str) -> None:
        release = find_helm_release(all_helm_releases, "kube-prometheus-stack")
        assert release is not None, "Helm release 'kube-prometheus-stack' not found"
        status = release.get("status", "")
        assert status == "deployed", f"kube-prometheus-stack status is '{status}', expected 'deployed'"


# ============================================================================
# node-exporter DaemonSet
# ============================================================================


class TestNodeExporter:
    """Verify node-exporter DaemonSet runs on all nodes."""

    def test_node_exporter_healthy(self, all_daemonsets: dict, all_nodes: dict, mon_ns: str) -> None:
        assert_daemonset_healthy(all_daemonsets, all_nodes, mon_ns, name_contains="node-exporter")


# ============================================================================
# kube-state-metrics Deployment
# ============================================================================


class TestKubeStateMetrics:
    """Verify kube-state-metrics Deployment is running."""

    def test_kube_state_metrics_ready(self, all_deployments: dict, mon_ns: str) -> None:
        deps = [
            d
            for d in filter_deployments(all_deployments, namespace=mon_ns)
            if "kube-state-metrics" in d.get("metadata", {}).get("name", "")
        ]
        assert len(deps) >= 1, f"No kube-state-metrics Deployment found in namespace '{mon_ns}'"
        ready = deps[0].get("status", {}).get("readyReplicas", 0)
        assert ready >= 1, f"kube-state-metrics: expected >= 1 ready replica, got {ready}"


# ============================================================================
# Prometheus Operator Deployment
# ============================================================================


class TestPrometheusOperator:
    """Verify Prometheus Operator Deployment is running."""

    def test_operator_ready(self, all_deployments: dict, mon_ns: str) -> None:
        deps = [
            d
            for d in filter_deployments(all_deployments, namespace=mon_ns)
            if "operator" in d.get("metadata", {}).get("name", "")
        ]
        assert len(deps) >= 1, f"No Prometheus Operator Deployment found in namespace '{mon_ns}'"
        ready = deps[0].get("status", {}).get("readyReplicas", 0)
        assert ready >= 1, f"Prometheus Operator: expected >= 1 ready replica, got {ready}"


# ============================================================================
# ServiceMonitors
# ============================================================================


class TestServiceMonitors:
    """Verify expected ServiceMonitors exist."""

    def test_service_monitors_exist(self, mon_ns: str) -> None:
        result = run_kubectl(["get", "servicemonitors"], namespace=mon_ns)
        sm_names = {item["metadata"]["name"] for item in result.get("items", [])}

        missing = [name for name in EXPECTED_SERVICE_MONITORS if name not in sm_names]
        assert not missing, f"Missing ServiceMonitors in '{mon_ns}': {missing}"


# ============================================================================
# PodMonitors
# ============================================================================


class TestPodMonitors:
    """Verify expected PodMonitors exist."""

    def test_pod_monitors_exist(self, mon_ns: str) -> None:
        result = run_kubectl(["get", "podmonitors"], namespace=mon_ns)
        pm_names = {item["metadata"]["name"] for item in result.get("items", [])}

        missing = [name for name in EXPECTED_POD_MONITORS if name not in pm_names]
        assert not missing, f"Missing PodMonitors in '{mon_ns}': {missing}"


# ============================================================================
# DCGM Exporter
# ============================================================================


class TestDCGMExporter:
    """Verify DCGM exporter DaemonSet exists (0 desired is OK if no GPU nodes)."""

    def test_dcgm_exporter_healthy(self, all_daemonsets: dict, all_nodes: dict, mon_ns: str) -> None:
        assert_daemonset_healthy(all_daemonsets, all_nodes, mon_ns, name_contains="dcgm", allow_zero=True)


# ============================================================================
# Alloy (conditional)
# ============================================================================


class TestAlloy:
    """Verify Alloy is deployed when grafana-cloud-credentials secret exists."""

    @pytest.fixture(autouse=True)
    def _require_credentials(self, mon_ns: str) -> None:
        """Skip all tests in this class if credentials secret is missing."""
        try:
            run_kubectl(["get", "secret", "grafana-cloud-credentials", "-o", "json"], namespace=mon_ns)
        except Exception:
            pytest.skip("grafana-cloud-credentials secret not found; Alloy not expected")

    def test_alloy_helm_release(self, all_helm_releases: list[dict]) -> None:
        release = find_helm_release(all_helm_releases, "alloy")
        assert release is not None, "Alloy Helm release not found but credentials exist"
        status = release.get("status", "")
        assert status == "deployed", f"Alloy status is '{status}', expected 'deployed'"

    def test_alloy_deployment_ready(self, all_deployments: dict, mon_ns: str) -> None:
        assert_deployment_ready(all_deployments, mon_ns, "alloy")


# ============================================================================
# Remote Verification (Grafana Cloud Mimir)
# ============================================================================


class TestMonitoringRemoteVerification:
    """Verify metrics are actually arriving in Grafana Cloud Mimir."""

    @pytest.fixture(autouse=True)
    def _require_remote(self, resolve_config, mon_ns: str):
        """Skip if no Mimir read URL configured or no read credentials."""
        read_url_base = resolve_config("monitoring.grafana_cloud_read_url", "")
        if not read_url_base:
            pytest.skip("No Mimir read URL configured (monitoring.grafana_cloud_read_url)")
        creds = fetch_grafana_cloud_credentials(
            mon_ns, "username", "password", secret_name="grafana-cloud-read-credentials"
        )
        if creds is None:
            pytest.skip("No grafana-cloud-read-credentials secret for monitoring")
        self.mimir_user, self.mimir_key = creds
        self.read_url = mimir_read_url(read_url_base)

    def test_metrics_arriving(self, resolve_config) -> None:
        """Query Mimir for the up metric from this cluster, verifying freshness.

        Checks that metrics were scraped within the last 5 minutes. This
        catches cases where a deploy broke Alloy but stale metrics still
        exist in Mimir from before the deploy.
        """
        import time

        max_staleness_seconds = 300  # 5 minutes

        cluster_name = resolve_config("cluster_name", "")
        if not cluster_name:
            pytest.skip("cluster_name not set in config")
        result = query_mimir(
            self.read_url,
            f'up{{cluster="{cluster_name}"}}',
            self.mimir_user,
            self.mimir_key,
        )
        if result is None:
            err = getattr(query_mimir, "last_error", "unknown")
            pytest.skip(f"Mimir query failed: {err}")
        status = result.get("status", "")
        assert status == "success", f"Mimir query returned status '{status}'"
        results = result.get("data", {}).get("result", [])
        assert len(results) > 0, f"No 'up' metrics found for cluster '{cluster_name}'"

        # Verify freshness: check that at least one sample is recent
        now = time.time()
        newest_ts = max(float(r["value"][0]) for r in results)
        age = now - newest_ts
        assert age < max_staleness_seconds, (
            f"Metrics are stale: newest sample is {age:.0f}s old "
            f"(threshold: {max_staleness_seconds}s). "
            f"Alloy may have stopped pushing after a deploy."
        )
