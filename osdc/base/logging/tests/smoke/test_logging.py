"""Smoke tests for centralized logging (Alloy DaemonSet + Events Deployment).

Validates that the logging namespace exists and, when grafana-cloud-credentials
are present, that the Alloy logging DaemonSet and events Deployment are deployed
and healthy.
"""

from __future__ import annotations

import re
import time

import pytest
from helpers import (
    READY_RETRIES,
    READY_RETRY_DELAY,
    assert_daemonset_healthy,
    assert_deployment_ready,
    assert_logs_fresh_in_loki,
    fetch_grafana_cloud_credentials,
    filter_pods,
    find_helm_release,
    get_unstable_node_names,
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

    def test_daemonset_ready(self, all_daemonsets: dict, all_nodes: dict, logging_ns: str) -> None:
        assert_daemonset_healthy(all_daemonsets, all_nodes, logging_ns, name_contains="alloy-logging")

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

    def test_configmap_has_module_pipelines(self, logging_ns: str) -> None:
        """Verify the ConfigMap contains module pipeline content beyond the base pipeline.

        The assemble_config.py script wraps each module's pipeline with comment
        delimiters of the form '// --- module: <name> ---'. Presence of at least
        one such delimiter confirms module pipeline injection ran and at least one
        module contributed a pipeline. This is stable — the delimiter format is
        controlled by assemble_config.py and does not change with module names.
        """
        result = run_kubectl(["get", "configmap", "alloy-logging-config", "-o", "json"], namespace=logging_ns)
        config = result.get("data", {}).get("config.alloy", "")
        assert "// --- module:" in config, (
            "ConfigMap contains no module pipeline comment delimiters ('// --- module: ...'). "
            "Either assemble_config.py was not run, or no cluster modules have logging/pipeline.alloy files."
        )

    def test_configmap_has_journal_pipeline(self, logging_ns: str) -> None:
        """Verify ConfigMap contains the journal log source for system log collection."""
        result = run_kubectl(["get", "configmap", "alloy-logging-config", "-o", "json"], namespace=logging_ns)
        config = result.get("data", {}).get("config.alloy", "")
        assert "loki.source.journal" in config, (
            "ConfigMap missing journal log source (loki.source.journal); system/kubelet logs will not be collected"
        )

    def test_configmap_has_level_normalization(self, logging_ns: str) -> None:
        """Verify ConfigMap contains level normalization stage.replace blocks.

        The base pipeline normalizes log levels in two places:
        - Pod logs: maps uppercase/abbreviated variants (ERROR, WARN, etc.) to lowercase
        - Journal: maps syslog PRIORITY integers to level strings

        We check that stage.replace blocks exist and that "error" and "warn" appear
        as replace targets, using regex to be resilient to HCL whitespace formatting.
        """
        result = run_kubectl(["get", "configmap", "alloy-logging-config", "-o", "json"], namespace=logging_ns)
        config = result.get("data", {}).get("config.alloy", "")
        assert re.search(r'replace\s*=\s*"error"', config), (
            'ConfigMap missing level normalization: no stage.replace targeting "error"'
        )
        assert re.search(r'replace\s*=\s*"warn"', config), (
            'ConfigMap missing level normalization: no stage.replace targeting "warn"'
        )

    def test_alloy_pods_running(self, all_pods: dict, all_nodes: dict, logging_ns: str) -> None:
        """Verify Alloy logging pods are in Running phase.

        Tolerates pods not Running on unstable nodes (new, NotReady, or being
        deleted) — these are expected during node churn (Karpenter scaling,
        spot interruptions, node recycling).

        Uses batch-fetched data first; if no pods found (e.g. nodes still
        joining after a recycle), retries with live kubectl fetches.
        """
        alloy_labels = {"app.kubernetes.io/instance": "alloy-logging"}
        pods = filter_pods(all_pods, namespace=logging_ns, labels=alloy_labels)
        nodes = all_nodes

        if not pods:
            # Batch data may be stale — retry with live fetches
            for _ in range(READY_RETRIES):
                time.sleep(READY_RETRY_DELAY)
                fresh = run_kubectl(["get", "pods", "-A"])
                pods = filter_pods(fresh, namespace=logging_ns, labels=alloy_labels)
                if pods:
                    break

        assert len(pods) > 0, f"No alloy-logging pods found (after {READY_RETRIES} retries)"

        unstable_names = get_unstable_node_names(nodes)
        not_running = [
            p["metadata"]["name"]
            for p in pods
            if p["status"].get("phase") != "Running" and p["spec"].get("nodeName") not in unstable_names
        ]

        if not not_running:
            return

        # Batch data may be stale — retry with live node + pod data
        for _ in range(READY_RETRIES):
            time.sleep(READY_RETRY_DELAY)
            fresh_pods = run_kubectl(["get", "pods", "-A"])
            fresh_nodes = run_kubectl(["get", "nodes"])
            pods = filter_pods(fresh_pods, namespace=logging_ns, labels=alloy_labels)
            unstable_names = get_unstable_node_names(fresh_nodes)
            not_running = [
                p["metadata"]["name"]
                for p in pods
                if p["status"].get("phase") != "Running" and p["spec"].get("nodeName") not in unstable_names
            ]
            if not not_running:
                return

        assert not not_running, (
            f"Alloy pods not Running on stable nodes: {not_running} "
            f"({len(unstable_names)} unstable nodes excluded, after {READY_RETRIES} retries)"
        )


# ============================================================================
# Alloy Events (conditional on credentials secret)
# ============================================================================


class TestAlloyEvents:
    """Verify Alloy events Deployment is deployed when credentials exist."""

    @pytest.fixture(autouse=True)
    def _require_credentials(self, logging_ns: str) -> None:
        """Skip all tests in this class if grafana-cloud-credentials secret is missing."""
        try:
            run_kubectl(["get", "secret", "grafana-cloud-credentials", "-o", "json"], namespace=logging_ns)
        except Exception:
            pytest.skip("grafana-cloud-credentials secret not found; alloy-events not expected")

    def test_helm_release_deployed(self, all_helm_releases: list[dict]) -> None:
        release = find_helm_release(all_helm_releases, "alloy-events")
        assert release is not None, "Helm release 'alloy-events' not found but grafana-cloud-credentials exists"
        status = release.get("status", "")
        assert status == "deployed", f"alloy-events status is '{status}', expected 'deployed'"

    def test_deployment_ready(self, all_deployments: dict, logging_ns: str) -> None:
        assert_deployment_ready(all_deployments, logging_ns, name="alloy-events")

    def test_events_pod_running(self, all_pods: dict, logging_ns: str) -> None:
        """Verify alloy-events pod is in Running phase."""
        alloy_labels = {"app.kubernetes.io/instance": "alloy-events"}
        pods = filter_pods(all_pods, namespace=logging_ns, labels=alloy_labels)

        if not pods:
            for _ in range(READY_RETRIES):
                time.sleep(READY_RETRY_DELAY)
                fresh = run_kubectl(["get", "pods", "-A"])
                pods = filter_pods(fresh, namespace=logging_ns, labels=alloy_labels)
                if pods:
                    break

        assert len(pods) > 0, f"No alloy-events pods found (after {READY_RETRIES} retries)"

        not_running = [p["metadata"]["name"] for p in pods if p["status"].get("phase") != "Running"]
        assert not not_running, f"Alloy events pods not Running: {not_running}"


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
        """Query Loki for recent logs from this cluster, verifying freshness.

        Checks that log entries were received within the last 5 minutes.
        This catches cases where a deploy broke the Alloy DaemonSet but
        stale log entries still exist in Loki from before the deploy.
        """
        max_staleness_seconds = 300  # 5 minutes

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

        # Verify freshness: check that at least one log entry is recent.
        # Loki query_range returns streams with "values": [[nanosecond_ts, line], ...]
        newest_ns = 0
        for stream in streams:
            for ts_ns, _ in stream.get("values", []):
                newest_ns = max(newest_ns, int(ts_ns))
        if newest_ns > 0:
            age = time.time() - (newest_ns / 1e9)
            assert age < max_staleness_seconds, (
                f"Logs are stale: newest entry is {age:.0f}s old "
                f"(threshold: {max_staleness_seconds}s). "
                f"Alloy DaemonSet may have stopped pushing after a deploy."
            )


# ============================================================================
# Per-Source Remote Verification (Grafana Cloud Loki)
# ============================================================================


class TestLoggingPerSourceVerification:
    """Verify each log source is producing fresh data in Grafana Cloud Loki."""

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

    def test_pod_logs_arriving(self, resolve_config) -> None:
        """Verify pod logs are being collected (kube-system always has pods)."""
        cluster_name = resolve_config("cluster_name", "")
        if not cluster_name:
            pytest.skip("cluster_name not set in config")

        logql = f'{{cluster="{cluster_name}", namespace="kube-system"}}'
        assert_logs_fresh_in_loki(
            self.read_url,
            logql,
            self.loki_user,
            self.loki_key,
            max_staleness=600,
            description="pod logs (kube-system)",
        )

    def test_journal_logs_arriving(self, resolve_config) -> None:
        """Verify journal logs are being collected (kubelet always runs)."""
        cluster_name = resolve_config("cluster_name", "")
        if not cluster_name:
            pytest.skip("cluster_name not set in config")

        logql = f'{{cluster="{cluster_name}", unit="kubelet.service"}}'
        assert_logs_fresh_in_loki(
            self.read_url,
            logql,
            self.loki_user,
            self.loki_key,
            max_staleness=600,
            description="journal logs (kubelet.service)",
        )

    def test_k8s_events_arriving(self, resolve_config) -> None:
        """Verify Kubernetes events are being collected (events always have reason label)."""
        cluster_name = resolve_config("cluster_name", "")
        if not cluster_name:
            pytest.skip("cluster_name not set in config")

        logql = f'{{cluster="{cluster_name}", reason=~".+"}}'
        assert_logs_fresh_in_loki(
            self.read_url,
            logql,
            self.loki_user,
            self.loki_key,
            max_staleness=600,
            description="k8s events",
        )
