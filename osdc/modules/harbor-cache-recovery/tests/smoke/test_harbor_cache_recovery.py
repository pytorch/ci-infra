"""Smoke tests for harbor-cache-recovery CronJob.

Validates that the CronJob, RBAC, and pod spec are correctly deployed.
"""

from __future__ import annotations

import pytest
from helpers import run_kubectl

pytestmark = [pytest.mark.live]

NAMESPACE = "harbor-system"
CRONJOB_NAME = "harbor-cache-recovery"
SA_NAME = "harbor-cache-recovery"
CLUSTERROLE_NAME = "harbor-cache-recovery"
CLUSTERROLEBINDING_NAME = "harbor-cache-recovery"
SECRET_NAME = "harbor-admin-password"  # noqa: S105


# ============================================================================
# CronJob
# ============================================================================


class TestCronJob:
    """Verify the harbor-cache-recovery CronJob exists and is configured."""

    @pytest.fixture(scope="class")
    def cronjob(self) -> dict:
        return run_kubectl(["get", "cronjob", CRONJOB_NAME], namespace=NAMESPACE)

    def test_exists(self, cronjob: dict) -> None:
        """CronJob must exist in harbor-system namespace."""
        assert cronjob["metadata"]["name"] == CRONJOB_NAME

    def test_schedule_not_placeholder(self, cronjob: dict) -> None:
        """Schedule must be resolved, not the deploy-time placeholder."""
        schedule = cronjob["spec"]["schedule"]
        assert "PLACEHOLDER" not in schedule, (
            f"CronJob schedule is still a placeholder: {schedule}. deploy.sh did not substitute SCHEDULE_PLACEHOLDER."
        )

    def test_concurrency_policy(self, cronjob: dict) -> None:
        """concurrencyPolicy must be Forbid to prevent overlapping runs."""
        policy = cronjob["spec"]["concurrencyPolicy"]
        assert policy == "Forbid", (
            f"concurrencyPolicy is {policy}, expected Forbid. Overlapping runs could cause duplicate Harbor API calls."
        )

    def test_not_suspended(self, cronjob: dict) -> None:
        """CronJob must not be suspended."""
        assert not cronjob["spec"].get("suspend", False), (
            "CronJob is suspended. Cache corruption will not be auto-recovered."
        )


# ============================================================================
# Job template pod spec
# ============================================================================


class TestPodSpec:
    """Verify the CronJob pod template has correct security and scheduling."""

    @pytest.fixture(scope="class")
    def pod_spec(self) -> dict:
        cj = run_kubectl(["get", "cronjob", CRONJOB_NAME], namespace=NAMESPACE)
        return cj["spec"]["jobTemplate"]["spec"]["template"]["spec"]

    @pytest.fixture(scope="class")
    def container_spec(self, pod_spec: dict) -> dict:
        containers = pod_spec["containers"]
        assert len(containers) == 1
        return containers[0]

    def test_service_account(self, pod_spec: dict) -> None:
        """Pod must use the harbor-cache-recovery ServiceAccount."""
        assert pod_spec["serviceAccountName"] == SA_NAME

    def test_restart_policy(self, pod_spec: dict) -> None:
        """Pod must have restartPolicy: Never for CronJob jobs."""
        assert pod_spec["restartPolicy"] == "Never"

    def test_node_selector(self, pod_spec: dict) -> None:
        """Pod must target base-infrastructure nodes."""
        selector = pod_spec.get("nodeSelector", {})
        assert selector.get("role") == "base-infrastructure", (
            f"nodeSelector is {selector}, expected role=base-infrastructure. CronJob may schedule on runner nodes."
        )

    def test_tolerates_critical_addons(self, pod_spec: dict) -> None:
        """Pod must tolerate CriticalAddonsOnly taint on base nodes."""
        tolerations = pod_spec.get("tolerations", [])
        has_toleration = any(
            t.get("key") == "CriticalAddonsOnly" and t.get("operator") == "Exists" and t.get("effect") == "NoSchedule"
            for t in tolerations
        )
        assert has_toleration, (
            "Missing CriticalAddonsOnly toleration. CronJob pods will be unschedulable on base-infrastructure nodes."
        )

    def test_run_as_non_root(self, pod_spec: dict) -> None:
        """Pod must run as non-root."""
        sc = pod_spec.get("securityContext", {})
        assert sc.get("runAsNonRoot") is True

    def test_container_security(self, container_spec: dict) -> None:
        """Container must have hardened security context."""
        sc = container_spec.get("securityContext", {})
        assert sc.get("readOnlyRootFilesystem") is True, "readOnlyRootFilesystem must be true"
        assert sc.get("allowPrivilegeEscalation") is False, "allowPrivilegeEscalation must be false"

    def test_image_not_placeholder(self, container_spec: dict) -> None:
        """Container image must be resolved, not the deploy-time placeholder."""
        image = container_spec.get("image", "")
        assert "PLACEHOLDER" not in image, (
            f"Container image is still a placeholder: {image}. "
            f"deploy.sh did not substitute HARBOR_CACHE_RECOVERY_IMAGE_PLACEHOLDER."
        )

    def test_env_vars_resolved(self, container_spec: dict) -> None:
        """All environment variables must be resolved (no PLACEHOLDER values)."""
        for env in container_spec.get("env", []):
            value = env.get("value", "")
            assert "PLACEHOLDER" not in value, f"Env var {env['name']} still has placeholder value: {value}"


# ============================================================================
# RBAC
# ============================================================================


class TestRBAC:
    """Verify ServiceAccount, ClusterRole, and ClusterRoleBinding exist."""

    def test_service_account_exists(self) -> None:
        """ServiceAccount must exist in harbor-system."""
        sa = run_kubectl(["get", "serviceaccount", SA_NAME], namespace=NAMESPACE)
        assert sa["metadata"]["name"] == SA_NAME

    def test_cluster_role_exists(self) -> None:
        """ClusterRole must exist with pod list permission."""
        cr = run_kubectl(["get", "clusterrole", CLUSTERROLE_NAME])
        rules = cr.get("rules", [])
        pod_rules = [r for r in rules if "pods" in r.get("resources", [])]
        assert pod_rules, (
            f"ClusterRole {CLUSTERROLE_NAME} has no rules for pods. "
            f"The script needs list permission to scan for ImagePullBackOff."
        )
        verbs = pod_rules[0].get("verbs", [])
        assert "list" in verbs, f"ClusterRole {CLUSTERROLE_NAME} is missing 'list' verb on pods. Has: {verbs}"

    def test_cluster_role_binding_exists(self) -> None:
        """ClusterRoleBinding must link SA to ClusterRole."""
        crb = run_kubectl(["get", "clusterrolebinding", CLUSTERROLEBINDING_NAME])
        role_ref = crb.get("roleRef", {})
        assert role_ref.get("name") == CLUSTERROLE_NAME, (
            f"ClusterRoleBinding references {role_ref.get('name')}, expected {CLUSTERROLE_NAME}"
        )
        subjects = crb.get("subjects", [])
        sa_subjects = [
            s
            for s in subjects
            if s.get("kind") == "ServiceAccount" and s.get("name") == SA_NAME and s.get("namespace") == NAMESPACE
        ]
        assert sa_subjects, (
            f"ClusterRoleBinding does not reference ServiceAccount {SA_NAME} in {NAMESPACE}. Subjects: {subjects}"
        )


# ============================================================================
# Secrets
# ============================================================================


class TestSecrets:
    """Verify required secrets exist."""

    def test_harbor_admin_password_exists(self) -> None:
        """harbor-admin-password secret must exist for CronJob auth."""
        secret = run_kubectl(["get", "secret", SECRET_NAME], namespace=NAMESPACE)
        assert secret["metadata"]["name"] == SECRET_NAME
        data_keys = list(secret.get("data", {}).keys())
        assert "password" in data_keys, f"Secret {SECRET_NAME} is missing 'password' key. Has: {data_keys}"
