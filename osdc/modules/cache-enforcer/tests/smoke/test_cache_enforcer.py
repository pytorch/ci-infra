"""Smoke tests for cache-enforcer DaemonSet.

Validates that the cache-enforcer DaemonSet is deployed and healthy in
kube-system, the ConfigMap contains the expected blocked domains, and the
pod spec is configured correctly (hostPID, hostNetwork, privileged,
nsenter, node affinity for runner nodes).
"""

from __future__ import annotations

import time

import pytest
from cache_enforcer_helpers import domain_in_variable_block, get_init_failures
from helpers import (
    READY_RETRIES,
    READY_RETRY_DELAY,
    assert_daemonset_healthy,
    filter_daemonsets,
    filter_pods,
    get_unstable_node_names,
    run_kubectl,
)

pytestmark = [pytest.mark.live]

NAMESPACE = "kube-system"
DAEMONSET_NAME = "cache-enforcer"
CONFIGMAP_NAME = "cache-enforcer-script"
CONFIGMAP_KEY = "enforce-cache.sh"

# Domains that MUST be blocked (from configmap.yaml).
EXPECTED_REGISTRY_DOMAINS = [
    "docker.io",
    "registry-1.docker.io",
    "auth.docker.io",
    "production.cloudflare.docker.com",
    "ghcr.io",
    "nvcr.io",
    "quay.io",
    "registry.k8s.io",
]

EXPECTED_PYPI_DOMAINS = [
    "pypi.org",
    "files.pythonhosted.org",
    "download.pytorch.org",
]

# Domains that must NOT be blocked (no rate limits, used for bootstrap).
ALLOWED_DOMAINS = [
    "public.ecr.aws",
]

# The DaemonSet only runs on Karpenter-managed runner nodes.
RUNNER_NODE_SELECTOR = {"workload-type": ["github-runner"]}

# Pod label selector for filtering cache-enforcer pods.
POD_LABELS = {"app": "cache-enforcer"}


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def ds_spec(all_daemonsets: dict) -> dict:
    """Return the cache-enforcer DaemonSet spec from batch-fetched data."""
    ds_list = filter_daemonsets(all_daemonsets, namespace=NAMESPACE, name=DAEMONSET_NAME)
    assert len(ds_list) >= 1, f"DaemonSet '{DAEMONSET_NAME}' not found in {NAMESPACE}"
    return ds_list[0]["spec"]["template"]["spec"]


@pytest.fixture(scope="session")
def configmap_script() -> str:
    """Fetch the enforce-cache.sh script from the ConfigMap."""
    result = run_kubectl(["get", "configmap", CONFIGMAP_NAME, "-o", "json"], namespace=NAMESPACE)
    data = result.get("data", {})
    assert CONFIGMAP_KEY in data, f"ConfigMap '{CONFIGMAP_NAME}' missing key '{CONFIGMAP_KEY}'"
    return data[CONFIGMAP_KEY]


# ============================================================================
# DaemonSet Health
# ============================================================================


class TestCacheEnforcerDaemonSet:
    """Verify cache-enforcer DaemonSet exists and is healthy."""

    def test_daemonset_exists(self, all_daemonsets: dict) -> None:
        ds_list = filter_daemonsets(all_daemonsets, namespace=NAMESPACE, name=DAEMONSET_NAME)
        assert len(ds_list) >= 1, f"DaemonSet '{DAEMONSET_NAME}' not found in {NAMESPACE}"

    def test_daemonset_healthy(self, all_daemonsets: dict, all_nodes: dict) -> None:
        """DaemonSet desired == ready, tolerating node churn.

        Uses node_selector so 0/0 is accepted when no runner nodes exist
        (e.g. Karpenter has scaled the runner pool to zero).
        """
        assert_daemonset_healthy(
            all_daemonsets,
            all_nodes,
            NAMESPACE,
            name=DAEMONSET_NAME,
            node_selector=RUNNER_NODE_SELECTOR,
        )


# ============================================================================
# ConfigMap Content
# ============================================================================


class TestCacheEnforcerConfigMap:
    """Verify the ConfigMap contains the expected domain blocking rules."""

    def test_configmap_exists(self, configmap_script: str) -> None:
        """ConfigMap exists and has non-empty script content."""
        assert len(configmap_script) > 0

    @pytest.mark.parametrize("domain", EXPECTED_REGISTRY_DOMAINS)
    def test_registry_domain_blocked(self, configmap_script: str, domain: str) -> None:
        """Each registry domain must appear in REGISTRY_DOMAINS."""
        assert domain in configmap_script, (
            f"Registry domain '{domain}' missing from {CONFIGMAP_NAME}. "
            f"Direct pulls to this registry will bypass Harbor cache."
        )

    @pytest.mark.parametrize("domain", EXPECTED_PYPI_DOMAINS)
    def test_pypi_domain_blocked(self, configmap_script: str, domain: str) -> None:
        """Each PyPI domain must appear in PYPI_DOMAINS."""
        assert domain in configmap_script, (
            f"PyPI domain '{domain}' missing from {CONFIGMAP_NAME}. pip/uv installs will bypass pypi-cache."
        )

    @pytest.mark.parametrize("domain", ALLOWED_DOMAINS)
    def test_allowed_domain_not_blocked(self, configmap_script: str, domain: str) -> None:
        """Allowed domains (e.g. public.ecr.aws) must NOT be in the blocked list."""
        # The domain might appear in comments — check it's not in the
        # REGISTRY_DOMAINS or PYPI_DOMAINS variable assignments.
        in_registry = domain_in_variable_block(configmap_script, "REGISTRY_DOMAINS", domain)
        in_pypi = domain_in_variable_block(configmap_script, "PYPI_DOMAINS", domain)
        assert not in_registry, (
            f"Domain '{domain}' found in REGISTRY_DOMAINS but should be allowed. "
            f"public.ecr.aws has no rate limits and must not be blocked."
        )
        assert not in_pypi, (
            f"Domain '{domain}' found in PYPI_DOMAINS but should be allowed. "
            f"public.ecr.aws has no rate limits and must not be blocked."
        )

    def test_xt_string_module_loaded(self, configmap_script: str) -> None:
        """Script must load the xt_string kernel module for SNI matching."""
        assert "modprobe xt_string" in configmap_script, (
            "Script missing 'modprobe xt_string' — iptables string matching will fail"
        )

    def test_reject_with_tcp_reset(self, configmap_script: str) -> None:
        """Rules must use tcp-reset for fast connection refused."""
        assert "tcp-reset" in configmap_script, (
            "Script missing 'tcp-reset' reject — blocked connections will timeout instead of failing fast"
        )

    def test_ipv6_support(self, configmap_script: str) -> None:
        """Script must attempt IPv6 rules (ip6tables)."""
        assert "ip6tables" in configmap_script, "Script missing IPv6 support (ip6tables)"

    def test_script_has_domain_blocks(self, configmap_script: str) -> None:
        """Script must define REGISTRY_DOMAINS and PYPI_DOMAINS variables."""
        assert 'REGISTRY_DOMAINS="' in configmap_script, "Missing REGISTRY_DOMAINS variable definition"
        assert 'PYPI_DOMAINS="' in configmap_script, "Missing PYPI_DOMAINS variable definition"


# ============================================================================
# Pod Spec Validation
# ============================================================================


class TestCacheEnforcerPodSpec:
    """Verify the DaemonSet pod spec has required security and scheduling config."""

    def test_host_network(self, ds_spec: dict) -> None:
        """Must use hostNetwork to install iptables rules on the node."""
        assert ds_spec.get("hostNetwork") is True, "hostNetwork must be true"

    def test_host_pid(self, ds_spec: dict) -> None:
        """Must use hostPID for nsenter access to host PID namespace."""
        assert ds_spec.get("hostPID") is True, "hostPID must be true"

    def test_priority_class(self, ds_spec: dict) -> None:
        """Must use system-node-critical to schedule before runner pods."""
        assert ds_spec.get("priorityClassName") == "system-node-critical", (
            "priorityClassName must be 'system-node-critical' — cache rules must be "
            "applied before any runner job pods can schedule"
        )

    def test_init_container_privileged(self, ds_spec: dict) -> None:
        """Init container must be privileged for iptables and nsenter."""
        init_containers = ds_spec.get("initContainers", [])
        assert len(init_containers) >= 1, "No init containers found"
        sc = init_containers[0].get("securityContext", {})
        assert sc.get("privileged") is True, "Init container must be privileged"

    def test_init_container_uses_nsenter(self, ds_spec: dict) -> None:
        """Init container must use nsenter to run in host mount namespace."""
        init_containers = ds_spec.get("initContainers", [])
        assert len(init_containers) >= 1, "No init containers found"
        args = init_containers[0].get("args", [])
        args_str = " ".join(args)
        assert "nsenter" in args_str, (
            "Init container args must use nsenter to execute in host namespace. "
            "Without nsenter, modprobe and iptables are unavailable in the minimal container image."
        )

    def test_node_affinity_targets_runners(self, ds_spec: dict) -> None:
        """Must target only runner nodes (workload-type: github-runner)."""
        affinity = ds_spec.get("affinity", {})
        node_affinity = affinity.get("nodeAffinity", {})
        required = node_affinity.get("requiredDuringSchedulingIgnoredDuringExecution", {})
        terms = required.get("nodeSelectorTerms", [])

        found = False
        for term in terms:
            for expr in term.get("matchExpressions", []):
                if expr.get("key") == "workload-type" and "github-runner" in expr.get("values", []):
                    found = True
                    break

        assert found, (
            "DaemonSet must have nodeAffinity requiring workload-type=github-runner. "
            "Without this, iptables rules would be installed on base infrastructure nodes."
        )

    def test_scripts_volume_mounted(self, ds_spec: dict) -> None:
        """Init container must mount the scripts ConfigMap."""
        init_containers = ds_spec.get("initContainers", [])
        assert len(init_containers) >= 1, "No init containers found"
        mounts = init_containers[0].get("volumeMounts", [])
        script_mounts = [m for m in mounts if m.get("name") == "scripts"]
        assert len(script_mounts) >= 1, "Init container missing 'scripts' volume mount"
        assert script_mounts[0].get("readOnly") is True, "Scripts volume should be mounted read-only"

    def test_tolerates_runner_node_taints(self, ds_spec: dict) -> None:
        """Must tolerate all taints used by Karpenter runner nodepools.

        At minimum: instance-type, git-cache-not-ready, nvidia.com/gpu.
        These taints are present on runner nodes and would prevent scheduling
        without matching tolerations.
        """
        tolerations = ds_spec.get("tolerations", [])
        toleration_keys = {t.get("key") for t in tolerations}

        required_keys = {
            "instance-type",
            "git-cache-not-ready",
            "nvidia.com/gpu",
            "cpu-type",
            "CriticalAddonsOnly",
        }
        missing = required_keys - toleration_keys
        assert not missing, (
            f"Missing tolerations for runner node taints: {missing}. "
            f"DaemonSet will not schedule on nodes with these taints."
        )


# ============================================================================
# Init Container Runtime Health
# ============================================================================


class TestCacheEnforcerInitContainerHealth:
    """Verify init containers completed successfully on running pods."""

    def test_init_containers_completed(self, all_pods: dict, all_nodes: dict) -> None:
        """All cache-enforcer init containers must have terminated with exit 0.

        Catches CrashLoopBackOff and non-zero exits that indicate the iptables
        script failed (missing kernel module, permission error, etc.).

        Tolerates pods on unstable nodes and transient init states
        (PodInitializing, ContainerCreating). Retries with live data.
        """
        pods = filter_pods(all_pods, namespace=NAMESPACE, labels=POD_LABELS)
        nodes = all_nodes

        if not pods:
            for _ in range(READY_RETRIES):
                time.sleep(READY_RETRY_DELAY)
                fresh = run_kubectl(["get", "pods", "-A"])
                pods = filter_pods(fresh, namespace=NAMESPACE, labels=POD_LABELS)
                if pods:
                    nodes = run_kubectl(["get", "nodes"])
                    break

        if not pods:
            pytest.skip("No cache-enforcer pods found (no runner nodes in cluster)")

        failures = get_init_failures(pods, nodes)
        if not failures:
            return

        # Batch data may be stale or init containers still running — retry
        for _ in range(READY_RETRIES):
            time.sleep(READY_RETRY_DELAY)
            fresh_pods = run_kubectl(["get", "pods", "-A"])
            fresh_nodes = run_kubectl(["get", "nodes"])
            pods = filter_pods(fresh_pods, namespace=NAMESPACE, labels=POD_LABELS)
            failures = get_init_failures(pods, fresh_nodes)
            if not failures:
                return

        assert not failures, (
            "Cache-enforcer init container failures on stable nodes:\n"
            + "\n".join(f"  - {f}" for f in failures)
            + "\n\nThe iptables enforcement script failed. Check pod logs for details."
        )

    def test_pods_running(self, all_pods: dict, all_nodes: dict) -> None:
        """All cache-enforcer pods on stable nodes must be in Running phase.

        Tolerates pods on unstable nodes. Retries with live data if batch
        data shows no pods (Karpenter may have just scaled up).
        """
        pods = filter_pods(all_pods, namespace=NAMESPACE, labels=POD_LABELS)
        nodes = all_nodes

        if not pods:
            for _ in range(READY_RETRIES):
                time.sleep(READY_RETRY_DELAY)
                fresh = run_kubectl(["get", "pods", "-A"])
                pods = filter_pods(fresh, namespace=NAMESPACE, labels=POD_LABELS)
                if pods:
                    break

        if not pods:
            pytest.skip("No cache-enforcer pods found (no runner nodes in cluster)")

        unstable_names = get_unstable_node_names(nodes)
        not_running = [
            p["metadata"]["name"]
            for p in pods
            if p["status"].get("phase") != "Running" and p["spec"].get("nodeName") not in unstable_names
        ]

        if not not_running:
            return

        # Batch data may be stale — retry with live fetches
        for _ in range(READY_RETRIES):
            time.sleep(READY_RETRY_DELAY)
            fresh_pods = run_kubectl(["get", "pods", "-A"])
            fresh_nodes = run_kubectl(["get", "nodes"])
            pods = filter_pods(fresh_pods, namespace=NAMESPACE, labels=POD_LABELS)
            unstable_names = get_unstable_node_names(fresh_nodes)
            not_running = [
                p["metadata"]["name"]
                for p in pods
                if p["status"].get("phase") != "Running" and p["spec"].get("nodeName") not in unstable_names
            ]
            if not not_running:
                return

        assert not not_running, (
            f"Cache-enforcer pods not Running on stable nodes: {not_running} "
            f"({len(unstable_names)} unstable nodes excluded, after {READY_RETRIES} retries)"
        )
