"""Smoke tests for VPC CNI Custom Networking ENIConfig CRs.

Two flavors of ENIConfig live side by side:

1. **AZ-named** (one per AZ, for base nodes). Every base node has an
   ``ipam.osdc.internal/eni-config`` label set by userData (see
   ``base/scripts/bootstrap/eks-base-pre-nodeadm-az-label.sh``) whose value
   matches the node's ``topology.kubernetes.io/zone`` label. There is one
   ``ENIConfig`` CR per available AZ, named exactly after the AZ string
   (e.g. ``us-east-2a``).

2. **Bucket-named** (one per (bucket, AZ) pair, for Karpenter workload
   nodes). Names are ``bucket-N-AZ`` (e.g. ``bucket-1-us-east-2a``); subnet
   IDs come from terraform's ``pod_subnets_by_bucket_az`` output.

These resources are inert until VPC CNI Custom Networking is enabled in a
later PR; this test verifies the prerequisites are in place and self-consistent.
"""

from __future__ import annotations

import subprocess

import pytest
from cni_constants import ENI_CONFIG_LABEL
from helpers import run_aws, run_kubectl

pytestmark = [pytest.mark.live]

ZONE_LABEL = "topology.kubernetes.io/zone"
BASE_ROLE_LABEL_VALUE = "base-infrastructure"


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture(scope="module")
def base_nodes(all_nodes: dict) -> list[dict]:
    """All nodes carrying ``role=base-infrastructure`` (base node AZ labeling target set)."""
    items = [
        n
        for n in all_nodes.get("items", [])
        if n.get("metadata", {}).get("labels", {}).get("role") == BASE_ROLE_LABEL_VALUE
    ]
    assert items, "No base-infrastructure nodes found — cannot validate AZ-named ENIConfig invariants"
    return items


@pytest.fixture(scope="module")
def base_node_azs(base_nodes: list[dict]) -> set[str]:
    """Set of AZs covered by the base node group (derived from node labels)."""
    azs = set()
    for node in base_nodes:
        az = node.get("metadata", {}).get("labels", {}).get(ZONE_LABEL)
        if az:
            azs.add(az)
    assert azs, f"Base nodes carry no '{ZONE_LABEL}' labels — cloud-controller-manager not running?"
    return azs


@pytest.fixture(scope="module")
def all_eniconfigs() -> dict:
    """All ENIConfig CRs in the cluster (returns empty items list if CRD missing)."""
    try:
        return run_kubectl(["get", "eniconfigs.crd.k8s.amazonaws.com"])
    except subprocess.CalledProcessError as exc:
        pytest.fail(
            f"Failed to list ENIConfig CRs (CRD missing or RBAC issue): {exc.stderr or exc}. "
            "AZ-named ENIConfig setup requires the ENIConfig CRD installed by the VPC CNI addon."
        )


@pytest.fixture(scope="module")
def pod_cidr_buckets(cluster_config: dict) -> dict:
    """`pod_cidr_buckets` map from clusters.yaml; skip the test if absent."""
    base_cfg = cluster_config["cluster"].get("base") or {}
    buckets = base_cfg.get("pod_cidr_buckets") or {}
    if not buckets:
        pytest.skip("This cluster has no pod_cidr_buckets configured")
    return buckets


@pytest.fixture(scope="module")
def expected_bucket_eniconfig_names(pod_cidr_buckets: dict) -> set[str]:
    """Set of expected `bucket-N-AZ` ENIConfig CR names derived from `pod_cidr_buckets`."""
    return {f"{bucket}-{az}" for bucket, az_map in pod_cidr_buckets.items() for az in az_map}


# ============================================================================
# Part A: base node label-AZ consistency
# ============================================================================


class TestBaseNodeENIConfigLabel:
    """Every base node has eni-config label equal to its zone label."""

    def test_base_nodes_have_zone_label(self, base_nodes: list[dict]) -> None:
        """Every base node has ``topology.kubernetes.io/zone`` (set by cloud-controller-manager)."""
        missing = [
            n["metadata"]["name"] for n in base_nodes if not n.get("metadata", {}).get("labels", {}).get(ZONE_LABEL)
        ]
        assert not missing, (
            f"Base nodes missing '{ZONE_LABEL}' label (cloud-controller-manager not labelling them?): {missing}"
        )

    def test_base_nodes_have_eni_config_label(self, base_nodes: list[dict]) -> None:
        """Every base node has ``ipam.osdc.internal/eni-config`` (set by base node AZ labeling userData)."""
        missing = [
            n["metadata"]["name"]
            for n in base_nodes
            if not n.get("metadata", {}).get("labels", {}).get(ENI_CONFIG_LABEL)
        ]
        assert not missing, (
            f"Base nodes missing '{ENI_CONFIG_LABEL}' label (base node AZ labeling userData drop-in not applied?): {missing}"
        )

    def test_base_node_eni_config_matches_zone(self, base_nodes: list[dict]) -> None:
        """eni-config label value equals zone label value on every base node.

        A mismatch means the userData script picked up a different AZ than
        cloud-controller-manager — usually means the node was relabeled
        manually or the IMDS read returned the wrong zone at first boot.
        Once VPC CNI Custom Networking is enabled, mismatched nodes will
        fail pod IP allocation with ``ErrNoENIConfig``.
        """
        mismatches: list[str] = []
        for node in base_nodes:
            labels = node.get("metadata", {}).get("labels", {})
            zone = labels.get(ZONE_LABEL, "")
            eni = labels.get(ENI_CONFIG_LABEL, "")
            if zone != eni:
                mismatches.append(f"{node['metadata']['name']}: zone={zone!r} eni-config={eni!r}")
        assert not mismatches, "Base nodes have eni-config != zone (AZ labeling invariant violated):\n  " + "\n  ".join(
            mismatches
        )


# ============================================================================
# Part B: ENIConfig CRD presence — one per base-node AZ
# ============================================================================


class TestBaseENIConfigCRsPresent:
    """One ENIConfig CR exists per AZ found on base nodes, named exactly the AZ."""

    def test_eniconfig_per_base_az(self, base_node_azs: set[str], all_eniconfigs: dict) -> None:
        """Every AZ a base node sits in has a same-named ``ENIConfig`` CR.

        Without this, the AZ-named ENIConfig setup is incomplete and pod
        restarts after the Custom Networking flip will fail with
        ``ErrNoENIConfig`` for the missing AZ.
        """
        existing = {ec["metadata"]["name"] for ec in all_eniconfigs.get("items", [])}
        missing = sorted(az for az in base_node_azs if az not in existing)
        assert not missing, (
            f"Missing ENIConfig CRs for base-node AZs: {missing}. "
            f"Existing ENIConfigs: {sorted(existing)}. "
            "AZ-named ENIConfig setup must create one CR per AZ (matching the AZ name exactly)."
        )

    def test_eniconfig_specs_have_subnet(self, base_node_azs: set[str], all_eniconfigs: dict) -> None:
        """Each AZ-named ENIConfig declares a non-empty ``spec.subnet``."""
        by_name = {ec["metadata"]["name"]: ec for ec in all_eniconfigs.get("items", [])}
        empty: list[str] = []
        for az in sorted(base_node_azs):
            ec = by_name.get(az)
            if ec is None:
                # Already covered by test_eniconfig_per_base_az
                continue
            subnet = ec.get("spec", {}).get("subnet", "")
            if not subnet:
                empty.append(az)
        assert not empty, (
            f"ENIConfig(s) missing 'spec.subnet': {empty}. "
            "AZ-named ENIConfig setup must point each AZ-named ENIConfig at the matching primary subnet."
        )


# ============================================================================
# Part C: ENIConfig subnet AZ matches ENIConfig name (cross-checked via AWS)
# ============================================================================


class TestBaseENIConfigSubnetAZ:
    """Each ENIConfig's spec.subnet lives in the AZ matching the ENIConfig's name.

    This catches the silent failure mode where an ENIConfig points at a subnet
    in the wrong AZ — pods will be unable to receive an IP because ENIs are
    AZ-bound and VPC CNI cannot attach a same-AZ ENI to the node.
    """

    @pytest.mark.aws
    def test_eniconfig_subnet_in_matching_az(
        self,
        base_node_azs: set[str],
        all_eniconfigs: dict,
        cluster_config: dict,
    ) -> None:
        region = cluster_config["cluster"].get("region", "us-west-2")
        by_name = {ec["metadata"]["name"]: ec for ec in all_eniconfigs.get("items", [])}

        # Collect the subnet IDs we need to look up in one batch — one
        # describe-subnets call beats N round-trips and keeps the test fast.
        subnets_to_check: dict[str, str] = {}  # az -> subnet_id
        for az in sorted(base_node_azs):
            ec = by_name.get(az)
            if ec is None:
                continue
            subnet = ec.get("spec", {}).get("subnet", "")
            if subnet:
                subnets_to_check[az] = subnet

        if not subnets_to_check:
            pytest.fail(
                "No ENIConfig subnets to verify — earlier tests should have caught this; "
                "indicates either no base-node AZs or no ENIConfig CRs."
            )

        result = run_aws(
            [
                "ec2",
                "describe-subnets",
                "--region",
                region,
                "--subnet-ids",
                *subnets_to_check.values(),
            ],
            timeout=120,
        )
        az_by_subnet = {s["SubnetId"]: s.get("AvailabilityZone", "") for s in result.get("Subnets", [])}

        mismatches: list[str] = []
        for az, subnet_id in sorted(subnets_to_check.items()):
            actual_az = az_by_subnet.get(subnet_id, "")
            if not actual_az:
                mismatches.append(f"ENIConfig {az!r}: subnet {subnet_id} not returned by describe-subnets")
            elif actual_az != az:
                mismatches.append(
                    f"ENIConfig {az!r}: spec.subnet {subnet_id} lives in AZ {actual_az!r}, expected {az!r}"
                )
        assert not mismatches, (
            "ENIConfig name <-> subnet AZ mismatch (would break pod IP allocation under Custom Networking):\n  "
            + "\n  ".join(mismatches)
        )


# ============================================================================
# Part D: bucket-named ENIConfig CRD presence — one per (bucket, AZ) pair
# ============================================================================


class TestBucketENIConfigCRsPresent:
    """One ENIConfig CR exists per (bucket, AZ) pair from pod_cidr_buckets, named ``bucket-N-AZ``."""

    def test_bucket_eniconfig_per_pair(self, expected_bucket_eniconfig_names: set[str], all_eniconfigs: dict) -> None:
        """Every (bucket, AZ) pair from ``pod_cidr_buckets`` has a same-named ``ENIConfig`` CR.

        Pod-bucket Custom Networking selects a per-(bucket, AZ) ENIConfig by
        node label. A missing CR means runner pods landing on that bucket+AZ
        will fail IP allocation with ``ErrNoENIConfig``.
        """
        actual = {
            ec["metadata"]["name"]
            for ec in all_eniconfigs.get("items", [])
            if ec["metadata"]["name"].startswith("bucket-")
        }
        missing = expected_bucket_eniconfig_names - actual
        assert not missing, (
            f"Missing bucket ENIConfigs: {sorted(missing)}. "
            f"Existing bucket ENIConfigs: {sorted(actual)}. "
            "Run base deploy first (eniconfigs creates one CR per (bucket, AZ) pair from pod_cidr_buckets)."
        )

    def test_bucket_eniconfig_specs_have_subnet(
        self, expected_bucket_eniconfig_names: set[str], all_eniconfigs: dict
    ) -> None:
        """Each bucket-named ENIConfig declares a non-empty ``spec.subnet`` starting with ``subnet-``."""
        by_name = {ec["metadata"]["name"]: ec for ec in all_eniconfigs.get("items", [])}
        bad: list[str] = []
        for name in sorted(expected_bucket_eniconfig_names):
            ec = by_name.get(name)
            if ec is None:
                # Already covered by test_bucket_eniconfig_per_pair
                continue
            subnet = ec.get("spec", {}).get("subnet", "")
            if not subnet or not subnet.startswith("subnet-"):
                bad.append(f"{name}: spec.subnet={subnet!r}")
        assert not bad, "Bucket ENIConfig(s) with empty or malformed 'spec.subnet':\n  " + "\n  ".join(bad)


# ============================================================================
# Part E: bucket ENIConfig subnet AZ + CIDR matches clusters.yaml (cross-checked via AWS)
# ============================================================================


class TestBucketENIConfigSubnetAZ:
    """Each bucket ENIConfig's spec.subnet lives in the matching AZ and has the expected CIDR.

    Catches the silent failure mode where a bucket ENIConfig points at a
    subnet in the wrong AZ (pods cannot get an IP — ENIs are AZ-bound) or
    at a subnet whose CIDR drifted from clusters.yaml (pod IP exhaustion
    won't match what capacity planning predicted).
    """

    @pytest.mark.aws
    def test_bucket_eniconfig_subnet_in_matching_az(
        self,
        pod_cidr_buckets: dict,
        cluster_config: dict,
        all_eniconfigs: dict,
    ) -> None:
        region = cluster_config["cluster"].get("region", "us-west-2")
        by_name = {ec["metadata"]["name"]: ec for ec in all_eniconfigs.get("items", [])}

        # Single batched describe-subnets beats N round-trips and keeps the test fast.
        to_check: dict[str, tuple[str, str, str]] = {}
        for bucket, az_map in pod_cidr_buckets.items():
            for az, expected_cidr in az_map.items():
                name = f"{bucket}-{az}"
                ec = by_name.get(name)
                if ec is None:
                    continue
                subnet = ec.get("spec", {}).get("subnet", "")
                if subnet:
                    to_check[name] = (az, expected_cidr, subnet)

        if not to_check:
            pytest.fail(
                "No bucket ENIConfig subnets to verify — earlier tests should have caught this; "
                "indicates either no pod_cidr_buckets or no bucket ENIConfig CRs."
            )

        result = run_aws(
            [
                "ec2",
                "describe-subnets",
                "--region",
                region,
                "--subnet-ids",
                *(subnet for _, _, subnet in to_check.values()),
            ],
            timeout=120,
        )
        info_by_subnet = {
            s["SubnetId"]: (s.get("AvailabilityZone", ""), s.get("CidrBlock", "")) for s in result.get("Subnets", [])
        }

        mismatches: list[str] = []
        for name, (expected_az, expected_cidr, subnet_id) in sorted(to_check.items()):
            info = info_by_subnet.get(subnet_id)
            if info is None:
                mismatches.append(f"ENIConfig {name!r}: subnet {subnet_id} not returned by describe-subnets")
                continue
            actual_az, actual_cidr = info
            if actual_az != expected_az:
                mismatches.append(
                    f"ENIConfig {name!r}: spec.subnet {subnet_id} lives in AZ {actual_az!r}, expected {expected_az!r}"
                )
            if actual_cidr != expected_cidr:
                mismatches.append(
                    f"ENIConfig {name!r}: spec.subnet {subnet_id} CIDR {actual_cidr!r}, "
                    f"expected {expected_cidr!r} (drifted from clusters.yaml pod_cidr_buckets)"
                )
        assert not mismatches, (
            "Bucket ENIConfig <-> subnet AZ/CIDR mismatch (would break pod IP allocation under Custom Networking):\n  "
            + "\n  ".join(mismatches)
        )
