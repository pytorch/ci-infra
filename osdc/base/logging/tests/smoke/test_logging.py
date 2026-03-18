"""Smoke tests for centralized logging (Alloy DaemonSet).

Validates that the logging namespace exists and, when grafana-cloud-credentials
are present, that the Alloy logging DaemonSet is deployed and healthy.
"""

from __future__ import annotations

import time

import pytest
from helpers import (
    READY_RETRIES,
    READY_RETRY_DELAY,
    assert_daemonset_ready,
    fetch_grafana_cloud_credentials,
    filter_pods,
    find_helm_release,
    loki_read_url,
    query_loki,
    run_kubectl,
)

pytestmark = [pytest.mark.live]


@pytest.fixture
def logging_ns(resolve_config) -> str:
    """Resolve the logging namespace from cluster config."""
    return resolve_config("logging.namespace", "logging")


# ============================================================================
# Namespace
# ============================================================================


class TestLoggingNamespace:
    """Verify the logging namespace exists."""

    def test_namespace_exists(self, all_namespaces: dict, logging_ns: str) -> None:
        ns_names = {item["metadata"]["name"] for item in all_namespaces.get("items", [])}
        assert logging_ns in ns_names, f"Namespace '{logging_ns}' not found"


# ============================================================================
# Alloy Logging (conditional on credentials secret)
# ============================================================================


class TestAlloyLogging:
    """Verify Alloy logging DaemonSet is deployed when credentials exist."""

    @pytest.fixture(autouse=True)
    def _require_credentials(self, logging_ns: str) -> None:
        """Skip all tests in this class if grafana-cloud-credentials secret is missing."""
        try:
            run_kubectl(["get", "secret", "grafana-cloud-credentials", "-o", "json"], namespace=logging_ns)
        except Exception:
            pytest.skip("grafana-cloud-credentials secret not found; logging Alloy not expected")

    def test_helm_release_deployed(self, all_helm_releases: list[dict]) -> None:
        release = find_helm_release(all_helm_releases, "alloy-logging")
        assert release is not None, "Helm release 'alloy-logging' not found but grafana-cloud-credentials exists"
        status = release.get("status", "")
        assert status == "deployed", f"alloy-logging status is '{status}', expected 'deployed'"

    def test_daemonset_ready(self, all_daemonsets: dict, logging_ns: str) -> None:
        assert_daemonset_ready(all_daemonsets, logging_ns, name_contains="alloy-logging")

    def test_configmap_exists(self, logging_ns: str) -> None:
        result = run_kubectl(["get", "configmap", "alloy-logging-config", "-o", "json"], namespace=logging_ns)
        data = result.get("data", {})
        assert "config.alloy" in data, "ConfigMap 'alloy-logging-config' missing 'config.alloy' key"

    def test_configmap_has_pipeline_content(self, logging_ns: str) -> None:
        """Verify ConfigMap contains expected pipeline structure."""
        result = run_kubectl(["get", "configmap", "alloy-logging-config", "-o", "json"], namespace=logging_ns)
        config = result.get("data", {}).get("config.alloy", "")
        assert "loki.source.file" in config, "ConfigMap missing pod log source (loki.source.file)"
        assert "loki.write" in config, "ConfigMap missing Loki writer (loki.write)"

    def test_alloy_pods_running(self, all_pods: dict, logging_ns: str) -> None:
        """Verify Alloy logging pods are in Running phase.

        Uses batch-fetched data first; if no pods found (e.g. nodes still
        joining after a recycle), retries with live kubectl fetches.
        """
        alloy_labels = {"app.kubernetes.io/instance": "alloy-logging"}
        pods = filter_pods(all_pods, namespace=logging_ns, labels=alloy_labels)

        if not pods:
            # Batch data may be stale — retry with live fetches
            for _ in range(READY_RETRIES):
                time.sleep(READY_RETRY_DELAY)
                fresh = run_kubectl(["get", "pods", "-A"])
                pods = filter_pods(fresh, namespace=logging_ns, labels=alloy_labels)
                if pods:
                    break

        assert len(pods) > 0, f"No alloy-logging pods found (after {READY_RETRIES} retries)"
        not_running = [p["metadata"]["name"] for p in pods if p["status"].get("phase") != "Running"]
        assert not not_running, f"Alloy pods not Running: {not_running}"


# ============================================================================
# Remote Verification (Grafana Cloud Loki)
# ============================================================================


class TestLoggingRemoteVerification:
    """Verify logs are actually arriving in Grafana Cloud Loki."""

    @pytest.fixture(autouse=True)
    def _require_remote(self, resolve_config, logging_ns: str):
        """Skip if no Loki URL configured or no credentials."""
        self.loki_write_url = resolve_config("logging.grafana_cloud_loki_url", "")
        if not self.loki_write_url:
            pytest.skip("No Loki URL configured")
        creds = fetch_grafana_cloud_credentials(logging_ns, "loki-username", "loki-api-key-read")
        if creds is None:
            pytest.skip("No grafana-cloud-credentials for logging")
        self.loki_user, self.loki_key = creds
        self.read_url = loki_read_url(self.loki_write_url)

    def test_logs_arriving(self, resolve_config) -> None:
        """Query Loki for recent logs from this cluster."""
        cluster_name = resolve_config("cluster_name", "")
        if not cluster_name:
            pytest.skip("cluster_name not set in config")
        result = query_loki(
            self.read_url,
            f'{{cluster="{cluster_name}"}}',
            self.loki_user,
            self.loki_key,
        )
        if result is None:
            err = getattr(query_loki, "last_error", "unknown")
            pytest.skip(f"Loki query failed: {err}")
        status = result.get("status", "")
        assert status == "success", f"Loki query returned status '{status}'"
        streams = result.get("data", {}).get("result", [])
        assert len(streams) > 0, f"No log streams found for cluster '{cluster_name}' in last hour"
