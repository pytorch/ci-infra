"""Smoke tests for Karpenter autoscaler.

Validates that the Karpenter Helm release is deployed, controller pods are
running with the expected replica count, and AWS resources (IAM role, SQS
queue, EventBridge rules) exist.
"""

from __future__ import annotations

import pytest
from helpers import filter_pods, find_helm_release, run_aws

pytestmark = [pytest.mark.live, pytest.mark.aws]

NAMESPACE = "karpenter"


# ============================================================================
# Helm release
# ============================================================================


class TestKarpenterHelm:
    """Verify the Karpenter Helm release is deployed."""

    def test_helm_release_deployed(self, all_helm_releases: list[dict]) -> None:
        release = find_helm_release(all_helm_releases, "karpenter", namespace=NAMESPACE)
        assert release is not None, "Helm release 'karpenter' not found in 'karpenter' namespace"
        assert release["status"] == "deployed", (
            f"Helm release 'karpenter' status is '{release['status']}', expected 'deployed'"
        )


# ============================================================================
# Controller pods
# ============================================================================


class TestKarpenterPods:
    """Verify Karpenter controller pods are running."""

    def test_pods_running(self, all_pods: dict, resolve_config) -> None:
        expected_replicas = resolve_config("karpenter.replicas", 2)
        pods = filter_pods(all_pods, namespace=NAMESPACE, labels={"app.kubernetes.io/name": "karpenter"})
        running = [p for p in pods if p.get("status", {}).get("phase") == "Running"]
        assert len(running) == expected_replicas, (
            f"Expected {expected_replicas} Running Karpenter pods, found {len(running)}"
        )


# ============================================================================
# AWS resources
# ============================================================================


class TestKarpenterAWS:
    """Verify AWS resources created by the Karpenter terraform exist."""

    def test_iam_role_exists(self, cluster_config: dict) -> None:
        cluster_name = cluster_config["cluster"]["cluster_name"]
        role_name = f"{cluster_name}-karpenter-controller"
        result = run_aws(["iam", "get-role", "--role-name", role_name])
        assert result.get("Role", {}).get("RoleName") == role_name, f"IAM role '{role_name}' not found"

    def test_sqs_queue_exists(self, cluster_config: dict) -> None:
        cluster_name = cluster_config["cluster"]["cluster_name"]
        region = cluster_config["cluster"]["region"]
        queue_name = f"{cluster_name}-karpenter"
        try:
            result = run_aws(["sqs", "get-queue-url", "--queue-name", queue_name, "--region", region])
            assert "QueueUrl" in result, f"SQS queue '{queue_name}' response missing QueueUrl"
        except Exception as exc:
            pytest.fail(f"SQS queue '{queue_name}' not found: {exc}")

    def test_private_subnets_tagged_for_discovery(self, cluster_config: dict) -> None:
        """Every private subnet is a Karpenter subnet, including AZs past the EKS cluster's three."""
        cluster_name = cluster_config["cluster"]["cluster_name"]
        region = cluster_config["cluster"]["region"]
        cluster = run_aws(["eks", "describe-cluster", "--name", cluster_name, "--region", region])
        vpc_id = cluster["cluster"]["resourcesVpcConfig"]["vpcId"]
        result = run_aws(
            [
                "ec2",
                "describe-subnets",
                "--region",
                region,
                "--filters",
                f"Name=vpc-id,Values={vpc_id}",
                "Name=tag:Name,Values=*-private-*",
            ]
        )
        subnets = result.get("Subnets", [])
        assert subnets, f"No private subnets found in {vpc_id}"
        untagged = [
            f"{s['SubnetId']} ({s['AvailabilityZone']})"
            for s in subnets
            if {t["Key"]: t["Value"] for t in s.get("Tags", [])}.get("karpenter.sh/discovery") != cluster_name
        ]
        assert not untagged, (
            f"Private subnets missing karpenter.sh/discovery={cluster_name}: {untagged}. "
            f"Karpenter won't launch nodes there."
        )

    def test_eventbridge_rules_exist(self, cluster_config: dict) -> None:
        cluster_name = cluster_config["cluster"]["cluster_name"]
        region = cluster_config["cluster"]["region"]
        result = run_aws(["events", "list-rules", "--name-prefix", cluster_name, "--region", region])
        rules = result.get("Rules", [])
        assert len(rules) >= 4, (
            f"Expected at least 4 EventBridge rules with prefix '{cluster_name}', found {len(rules)}"
        )
