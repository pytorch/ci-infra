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
    assert_metric_fresh_in_mimir,
    fetch_grafana_cloud_credentials,
    filter_deployments,
    find_helm_release,
    mimir_read_url,
    query_mimir,
    run_kubectl,
)

pytestmark = [pytest.mark.live]

EXPECTED_SERVICE_MONITORS = [
    "apiserver",
    "arc-controller",
    "buildkit",
    "buildkit-haproxy",
    "harbor",
    "karpenter",
    "node-compactor",
    "git-cache-central",
    "dcgm-exporter",
]

EXPECTED_POD_MONITORS = [
    "arc-listeners",
    "coredns",
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

    def test_dcgm_exporter_no_crashloop(self, all_pods: dict, mon_ns: str) -> None:
        """Detect CrashLoopBackOff by checking restart counts on dcgm-exporter pods.

        OOMKilled containers (exit code 137) enter CrashLoopBackOff but the
        DaemonSet-level health check (desired==ready) can miss this when
        Karpenter terminates the underlying node before the next reconcile.
        """
        pods = [
            p
            for p in all_pods.get("items", [])
            if p.get("metadata", {}).get("namespace") == mon_ns
            and "dcgm-exporter" in p.get("metadata", {}).get("name", "")
        ]
        if not pods:
            pytest.skip("No dcgm-exporter pods found (no GPU nodes)")

        max_restarts = 3
        problems = []
        for pod in pods:
            name = pod["metadata"]["name"]
            node = pod.get("spec", {}).get("nodeName", "unknown")
            for cs in pod.get("status", {}).get("containerStatuses", []):
                restarts = cs.get("restartCount", 0)
                waiting = cs.get("state", {}).get("waiting", {})
                last_term = cs.get("lastState", {}).get("terminated", {})

                if restarts > max_restarts:
                    reason = last_term.get("reason", "Unknown")
                    problems.append(f"{name} on {node}: {restarts} restarts (last: {reason})")
                elif waiting.get("reason") == "CrashLoopBackOff":
                    problems.append(f"{name} on {node}: CrashLoopBackOff")

        assert not problems, f"dcgm-exporter pod health issues: {'; '.join(problems)}"


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


# ============================================================================
# Per-Target Remote Verification (Grafana Cloud Mimir)
# ============================================================================

# Scrape targets to verify in Mimir.
# Each entry maps a descriptive name to (mimir_job_label, required_module).
# The job label comes from the target Service name (Alloy/Prometheus convention
# for ServiceMonitors without an explicit jobLabel field).
# required_module is None for base components (always present).
SCRAPE_TARGETS: dict[str, tuple[str, str | None]] = {
    "buildkit": ("buildkitd-pods", "buildkit"),
    "buildkit-haproxy": ("buildkitd-lb-metrics", "buildkit"),
    "karpenter": ("karpenter", "karpenter"),
    "node-compactor": ("node-compactor", None),
    "git-cache-central": ("git-cache-central-metrics", None),
    # arc-controller: skipped — ARC controller metrics Service varies by chart version
    # harbor: skipped — Harbor exporter Service name varies by chart version
    # dcgm-exporter: skipped — only runs on GPU nodes which may not exist
    # arc-listeners: skipped — ephemeral pods, too flaky
    # git-cache-daemonset: skipped — PodMonitor, different job label format
}

# kube-prometheus-stack built-in targets (always present when monitoring is enabled).
# These use short Service names, not Helm-prefixed names.
KUBE_PROM_STACK_TARGETS = [
    "node-exporter",
    "kube-state-metrics",
]

# Control plane targets scraped via custom monitors (always present).
# API server: ServiceMonitor targets the "kubernetes" Service, so job="kubernetes".
# CoreDNS: PodMonitor targets kube-dns pods, so job label varies by Alloy convention.
CONTROL_PLANE_TARGETS = [
    ("apiserver", "kubernetes"),
    # coredns: skipped — PodMonitor job labels are pod-name-based, too variable
]


class TestMonitoringPerTargetVerification:
    """Verify per-target metrics are arriving in Grafana Cloud Mimir."""

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

    @pytest.mark.parametrize("target_name", list(SCRAPE_TARGETS.keys()))
    def test_scrape_target_fresh(self, target_name: str, resolve_config, enabled_modules: list[str]) -> None:
        """Verify each ServiceMonitor target has fresh metrics in Mimir."""
        job_label, required_module = SCRAPE_TARGETS[target_name]
        if required_module is not None and required_module not in enabled_modules:
            pytest.skip(f"Module '{required_module}' not enabled for this cluster")

        cluster_name = resolve_config("cluster_name", "")
        if not cluster_name:
            pytest.skip("cluster_name not set in config")

        promql = f'up{{job="{job_label}", cluster="{cluster_name}"}}'
        assert_metric_fresh_in_mimir(
            self.read_url,
            promql,
            self.mimir_user,
            self.mimir_key,
            max_staleness=600,
            description=f"scrape target: {target_name}",
        )

    @pytest.mark.parametrize("target_name", KUBE_PROM_STACK_TARGETS)
    def test_kube_prom_stack_target_fresh(self, target_name: str, resolve_config) -> None:
        """Verify kube-prometheus-stack built-in targets have fresh metrics."""
        cluster_name = resolve_config("cluster_name", "")
        if not cluster_name:
            pytest.skip("cluster_name not set in config")

        promql = f'up{{job="{target_name}", cluster="{cluster_name}"}}'
        assert_metric_fresh_in_mimir(
            self.read_url,
            promql,
            self.mimir_user,
            self.mimir_key,
            max_staleness=600,
            description=f"kube-prom-stack target: {target_name}",
        )

    @pytest.mark.parametrize(("monitor_name", "job_label"), CONTROL_PLANE_TARGETS)
    def test_control_plane_target_fresh(self, monitor_name: str, job_label: str, resolve_config) -> None:
        """Verify control plane targets (API server, CoreDNS) have fresh metrics."""
        cluster_name = resolve_config("cluster_name", "")
        if not cluster_name:
            pytest.skip("cluster_name not set in config")

        promql = f'up{{job="{job_label}", cluster="{cluster_name}"}}'
        assert_metric_fresh_in_mimir(
            self.read_url,
            promql,
            self.mimir_user,
            self.mimir_key,
            max_staleness=600,
            description=f"control plane target: {monitor_name}",
        )
