"""Smoke tests for EKS cluster and AWS infrastructure."""

import pytest
import yaml
from helpers import run_aws

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


class TestEKSAddons:
    """Verify required EKS addons are installed and active."""

    @pytest.mark.parametrize("addon_name", _REQUIRED_ADDONS)
    def test_addon_is_active(self, cluster_config, addon_name):
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
        assert status == "ACTIVE", f"Addon {addon_name} status is {status}, expected ACTIVE"


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
