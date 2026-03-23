"""Smoke tests for EKS cluster and AWS infrastructure."""

import pytest
import yaml
from helpers import get_unstable_node_names, run_aws, run_kubectl

pytestmark = [pytest.mark.live, pytest.mark.aws]


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture(scope="session")
def eks_cluster_info(cluster_config):
    """Fetch EKS cluster description once for all tests."""
    cluster_name = cluster_config["cluster"]["cluster_name"]
    region = cluster_config["cluster"].get("region", "us-west-2")
    return run_aws(["eks", "describe-cluster", "--name", cluster_name, "--region", region])


# ============================================================================
# EKS Cluster
# ============================================================================


class TestEKSCluster:
    """Verify the EKS cluster is healthy and configured correctly."""

    def test_cluster_is_active(self, eks_cluster_info, cluster_config):
        status = eks_cluster_info["cluster"]["status"]
        cluster_name = cluster_config["cluster"]["cluster_name"]
        assert status == "ACTIVE", f"EKS cluster {cluster_name} status is {status}, expected ACTIVE"

    def test_cluster_version(self, eks_cluster_info, resolve_config):
        expected_version = resolve_config("eks_version", "1.35")
        actual_version = eks_cluster_info["cluster"]["version"]
        assert actual_version == expected_version, (
            f"EKS version {actual_version} does not match expected {expected_version}"
        )

    def test_oidc_provider_configured(self, eks_cluster_info, cluster_config):
        cluster_name = cluster_config["cluster"]["cluster_name"]
        oidc_issuer = eks_cluster_info["cluster"].get("identity", {}).get("oidc", {}).get("issuer", "")
        assert oidc_issuer, f"OIDC provider not configured for cluster {cluster_name}"


# ============================================================================
# EKS Addons
# ============================================================================


_REQUIRED_ADDONS = ["vpc-cni", "coredns", "kube-proxy", "aws-ebs-csi-driver"]

# Terminal addon statuses that indicate a real failure (not transient churn).
_ADDON_FAILURE_STATUSES = {"CREATE_FAILED", "DELETE_FAILED"}

# Addons backed by DaemonSets — their pod-level health is the real signal.
# Maps addon name to (namespace, pod label selector key, pod label selector value).
_DAEMONSET_ADDONS: dict[str, tuple[str, str, str]] = {
    "kube-proxy": ("kube-system", "k8s-app", "kube-proxy"),
    "vpc-cni": ("kube-system", "k8s-app", "aws-node"),
    "aws-ebs-csi-driver": ("kube-system", "app", "ebs-csi-node"),
}


class TestEKSAddons:
    """Verify required EKS addons are installed and healthy.

    Uses a two-layer check:
    1. Addon must not be in a terminal failure state (CREATE_FAILED, DELETE_FAILED).
    2. For DaemonSet-backed addons (kube-proxy, vpc-cni, ebs-csi), verify actual
       pod health — all pods on stable nodes must be Running. This is resilient to
       transient DEGRADED status caused by Karpenter node churn.
    """

    @pytest.mark.parametrize("addon_name", _REQUIRED_ADDONS)
    def test_addon_healthy(self, cluster_config, addon_name):
        cluster_name = cluster_config["cluster"]["cluster_name"]
        region = cluster_config["cluster"].get("region", "us-west-2")
        result = run_aws(
            [
                "eks",
                "describe-addon",
                "--cluster-name",
                cluster_name,
                "--addon-name",
                addon_name,
                "--region",
                region,
            ],
            timeout=120,
        )
        status = result["addon"]["status"]
        assert status not in _ADDON_FAILURE_STATUSES, f"Addon {addon_name} is in terminal failure state: {status}"

        if status == "ACTIVE":
            return

        # Status is DEGRADED or UPDATING — verify actual pod health for
        # DaemonSet-backed addons (the most common case for transient DEGRADED).
        ds_info = _DAEMONSET_ADDONS.get(addon_name)
        if ds_info is None:
            # Non-DaemonSet addon (e.g. coredns) — can't do pod-level check,
            # so accept non-failure status.
            return

        ns, label_key, label_value = ds_info
        pods_result = run_kubectl(["get", "pods", "-l", f"{label_key}={label_value}"], namespace=ns)
        pods = pods_result.get("items", [])
        assert len(pods) > 0, f"Addon {addon_name}: no pods found with label {label_key}={label_value} in {ns}"

        nodes_result = run_kubectl(["get", "nodes"])
        unstable = get_unstable_node_names(nodes_result)

        not_running = [
            p["metadata"]["name"]
            for p in pods
            if p["status"].get("phase") != "Running" and p["spec"].get("nodeName") not in unstable
        ]
        assert not not_running, (
            f"Addon {addon_name} status is {status} and pods are unhealthy on stable nodes: "
            f"{not_running} ({len(unstable)} unstable nodes excluded)"
        )


# ============================================================================
# ECR Images
# ============================================================================


class TestECRImages:
    """Verify ECR repositories exist for all bootstrap images."""

    def test_ecr_repos_exist(self, cluster_config, upstream_dir):
        region = cluster_config["cluster"].get("region", "us-west-2")
        images_path = upstream_dir / "modules" / "eks" / "images.yaml"
        assert images_path.exists(), f"images.yaml not found at {images_path}"

        with open(images_path) as f:
            images_data = yaml.safe_load(f)

        expected_repos = [img["repository"] for img in images_data.get("images", [])]
        assert len(expected_repos) > 0, "No images defined in images.yaml"

        result = run_aws(["ecr", "describe-repositories", "--region", region])
        ecr_repo_names = [r["repositoryName"] for r in result.get("repositories", [])]

        missing = [repo for repo in expected_repos if repo not in ecr_repo_names]
        assert not missing, f"ECR repositories missing for bootstrap images: {missing}"
