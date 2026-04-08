"""Smoke tests for Harbor pull-through cache."""

import pytest
from helpers import filter_pods, find_helm_release, run_kubectl

pytestmark = [pytest.mark.live]

HARBOR_NAMESPACE = "harbor-system"


# ============================================================================
# Harbor Helm Release
# ============================================================================


class TestHarborHelm:
    """Verify Harbor Helm release is deployed."""

    def test_harbor_release_exists(self, all_helm_releases):
        release = find_helm_release(all_helm_releases, "harbor", namespace=HARBOR_NAMESPACE)
        assert release is not None, "Harbor Helm release not found in harbor-system"

    def test_harbor_release_deployed(self, all_helm_releases):
        release = find_helm_release(all_helm_releases, "harbor", namespace=HARBOR_NAMESPACE)
        assert release is not None, "Harbor Helm release not found"
        assert release["status"] == "deployed", f"Harbor release status is '{release['status']}', expected 'deployed'"


# ============================================================================
# Harbor Pods
# ============================================================================


_EXPECTED_COMPONENTS = ["core", "registry", "jobservice", "portal", "nginx", "redis", "database"]


class TestHarborPods:
    """Verify Harbor component pods are running."""

    def test_harbor_pods_running(self, all_pods):
        harbor_pods = filter_pods(all_pods, namespace=HARBOR_NAMESPACE, labels={"app": "harbor"})
        assert len(harbor_pods) > 0, "No Harbor pods found in harbor-system"
        for pod in harbor_pods:
            name = pod["metadata"]["name"]
            phase = pod["status"].get("phase", "Unknown")
            assert phase == "Running", f"Harbor pod {name} phase is {phase}, expected Running"

    def test_harbor_components_present(self, all_pods):
        harbor_pods = filter_pods(all_pods, namespace=HARBOR_NAMESPACE, labels={"app": "harbor"})
        pod_components = set()
        for pod in harbor_pods:
            component = pod.get("metadata", {}).get("labels", {}).get("component", "")
            if component:
                pod_components.add(component)
        missing = [c for c in _EXPECTED_COMPONENTS if c not in pod_components]
        assert not missing, f"Harbor components missing: {missing}. Found: {sorted(pod_components)}"


# ============================================================================
# Harbor Secrets
# ============================================================================


_REQUIRED_SECRETS = ["harbor-s3-credentials", "harbor-admin-password"]


class TestHarborSecrets:
    """Verify required Harbor secrets exist."""

    @pytest.mark.parametrize("secret_name", _REQUIRED_SECRETS)
    def test_secret_exists(self, secret_name):
        result = run_kubectl(["get", "secret", secret_name], namespace=HARBOR_NAMESPACE)
        assert result["metadata"]["name"] == secret_name


# ============================================================================
# Harbor Service Account
# ============================================================================


class TestHarborServiceAccount:
    """Verify Harbor registry service account with IRSA."""

    @pytest.fixture
    def harbor_sa(self):
        return run_kubectl(["get", "serviceaccount", "harbor-registry"], namespace=HARBOR_NAMESPACE)

    def test_service_account_exists(self, harbor_sa):
        assert harbor_sa["metadata"]["name"] == "harbor-registry"

    def test_service_account_has_irsa_annotation(self, harbor_sa):
        annotations = harbor_sa.get("metadata", {}).get("annotations", {})
        irsa_key = "eks.amazonaws.com/role-arn"
        assert irsa_key in annotations, (
            f"Harbor registry SA missing IRSA annotation ({irsa_key}). Annotations: {annotations}"
        )
        assert annotations[irsa_key].startswith("arn:aws:iam::"), (
            f"IRSA annotation value doesn't look like an IAM role ARN: {annotations[irsa_key]}"
        )


# ============================================================================
# Harbor Proxy Cache Projects
# ============================================================================


_EXPECTED_PROJECTS = [
    "dockerhub-cache",
    "ghcr-cache",
    "ecr-public-cache",
    "nvcr-cache",
    "k8s-cache",
    "quay-cache",
]


class TestHarborProxyCacheProjects:
    """Verify Harbor proxy cache projects are expected (requires running Harbor)."""

    def test_harbor_core_running_for_proxy_projects(self, all_pods):
        """Proxy cache projects require a running Harbor core. Verify it is up."""
        harbor_pods = filter_pods(all_pods, namespace=HARBOR_NAMESPACE, labels={"app": "harbor"})
        core_pods = [p for p in harbor_pods if p.get("metadata", {}).get("labels", {}).get("component") == "core"]
        assert len(core_pods) > 0, "No Harbor core pods found -- proxy cache projects cannot function"
        for pod in core_pods:
            phase = pod["status"].get("phase", "Unknown")
            assert phase == "Running", f"Harbor core pod {pod['metadata']['name']} is {phase}"
