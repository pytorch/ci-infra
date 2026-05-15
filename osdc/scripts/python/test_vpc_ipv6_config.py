"""Unit tests for the VPC submodule IPv6 dual-stack configuration.

Parses modules/eks/terraform/modules/vpc/{main.tf,outputs.tf} as text and
asserts the IPv6 resources, attributes, and outputs are present. Cheaper
than spinning up tofu and stable enough for the structural checks we want.
"""

import re
from pathlib import Path

import pytest

VPC_MODULE_DIR = Path(__file__).resolve().parents[2] / "modules" / "eks" / "terraform" / "modules" / "vpc"
EKS_MODULE_DIR = Path(__file__).resolve().parents[2] / "modules" / "eks" / "terraform" / "modules" / "eks"


@pytest.fixture(scope="module")
def vpc_main_tf() -> str:
    return (VPC_MODULE_DIR / "main.tf").read_text()


@pytest.fixture(scope="module")
def vpc_outputs_tf() -> str:
    return (VPC_MODULE_DIR / "outputs.tf").read_text()


@pytest.fixture(scope="module")
def eks_main_tf() -> str:
    return (EKS_MODULE_DIR / "main.tf").read_text()


class TestVPCIPv6:
    """aws_vpc.this gets an AWS-generated /56 IPv6 CIDR."""

    def test_vpc_assigns_ipv6_block(self, vpc_main_tf):
        assert "assign_generated_ipv6_cidr_block = true" in vpc_main_tf, (
            "aws_vpc.this must request an AWS-generated IPv6 /56 — EKS IPv6 mode requires the VPC to have an IPv6 CIDR."
        )


class TestSubnetIPv6:
    """Both subnet families carve an IPv6 /64 from the VPC /56."""

    def test_public_subnet_has_ipv6_cidr(self, vpc_main_tf):
        # Offset by 100 to keep public IPv6 prefixes disjoint from private.
        assert "cidrsubnet(aws_vpc.this.ipv6_cidr_block, 8, count.index + 100)" in vpc_main_tf

    def test_private_subnet_has_ipv6_cidr(self, vpc_main_tf):
        assert "cidrsubnet(aws_vpc.this.ipv6_cidr_block, 8, count.index)" in vpc_main_tf

    def test_subnets_assign_ipv6_on_creation(self, vpc_main_tf):
        # Both public and private subnets must auto-assign IPv6 to ENIs.
        assert vpc_main_tf.count("assign_ipv6_address_on_creation = true") >= 2


class TestEgressOnlyIGW:
    """IPv6 outbound from private subnets requires an EIGW (no NAT for v6)."""

    def test_eigw_resource_exists(self, vpc_main_tf):
        assert 'resource "aws_egress_only_internet_gateway" "this"' in vpc_main_tf


class TestRouteTablesIPv6:
    """::/0 routes wired to IGW (public) and EIGW (private)."""

    def test_public_route_table_has_ipv6_default(self, vpc_main_tf):
        assert 'ipv6_cidr_block = "::/0"' in vpc_main_tf
        assert "gateway_id      = aws_internet_gateway.this.id" in vpc_main_tf

    def test_private_route_table_has_ipv6_default_via_eigw(self, vpc_main_tf):
        assert "egress_only_gateway_id = aws_egress_only_internet_gateway.this.id" in vpc_main_tf


class TestOutputs:
    """Downstream needs the VPC IPv6 CIDR and EIGW id for visibility."""

    def test_vpc_ipv6_cidr_block_output(self, vpc_outputs_tf):
        assert 'output "vpc_ipv6_cidr_block"' in vpc_outputs_tf
        assert "aws_vpc.this.ipv6_cidr_block" in vpc_outputs_tf

    def test_eigw_id_output(self, vpc_outputs_tf):
        assert 'output "egress_only_internet_gateway_id"' in vpc_outputs_tf
        assert "aws_egress_only_internet_gateway.this.id" in vpc_outputs_tf


# ---------------------------------------------------------------------------
# EKS module — aws_eks_addon.vpc_cni configuration_values
# ---------------------------------------------------------------------------


def _extract_resource_block(text: str, resource_type: str, resource_name: str) -> str:
    """Return the body of the named HCL resource block (between { and matching })."""
    res_match = re.search(rf'resource\s+"{re.escape(resource_type)}"\s+"{re.escape(resource_name)}"\s*\{{', text)
    assert res_match, f"{resource_type}.{resource_name} resource not found"
    start = res_match.end()
    depth = 1
    i = start
    while i < len(text) and depth > 0:
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        i += 1
    return text[start : i - 1]


class TestVPCCNIAddonIPv6:
    """aws_eks_addon.vpc_cni configuration_values must enable IPv6 on both
    the main aws-node container and the aws-vpc-cni-init init container.

    The configuration_values is a jsonencode(HCL-object) — instead of parsing
    HCL into JSON (fragile), we assert the key/value text appears in the
    aws_eks_addon.vpc_cni resource block.
    """

    def test_main_container_enables_ipv6(self, eks_main_tf):
        block = _extract_resource_block(eks_main_tf, "aws_eks_addon", "vpc_cni")
        assert re.search(r'ENABLE_IPv6\s*=\s*"true"', block), 'aws_eks_addon.vpc_cni env must set ENABLE_IPv6 = "true"'
        assert re.search(r'ENABLE_IPv4\s*=\s*"false"', block), (
            'aws_eks_addon.vpc_cni env must set ENABLE_IPv4 = "false"'
        )

    def test_init_container_enables_ipv6(self, eks_main_tf):
        # Per AWS docs, ENABLE_IPv6 must be set on BOTH the main aws-node
        # container and the aws-vpc-cni-init init container — the init
        # container sets kernel sysctls and needs the same flag. We look
        # for an `init = { env = { ... ENABLE_IPv6 = "true" ... } }` shape.
        block = _extract_resource_block(eks_main_tf, "aws_eks_addon", "vpc_cni")
        init_match = re.search(r"init\s*=\s*\{(.*?)\}\s*\}", block, re.DOTALL)
        assert init_match, (
            "aws_eks_addon.vpc_cni configuration_values must define an "
            'init = { env = { ENABLE_IPv6 = "true" } } block for the '
            "aws-vpc-cni-init init container"
        )
        init_body = init_match.group(0)
        assert re.search(r'ENABLE_IPv6\s*=\s*"true"', init_body), (
            'aws_eks_addon.vpc_cni init.env must set ENABLE_IPv6 = "true"'
        )

    def test_v4_egress_is_explicit_true(self, eks_main_tf):
        # ENABLE_V4_EGRESS default is true since aws-vpc-cni v1.15.1, but
        # we pin it explicitly so a default flip cannot silently disable
        # IPv4 egress for outbound to IPv4-only services (github.com, etc).
        block = _extract_resource_block(eks_main_tf, "aws_eks_addon", "vpc_cni")
        assert re.search(r'ENABLE_V4_EGRESS\s*=\s*"true"', block), (
            'aws_eks_addon.vpc_cni env must explicitly set ENABLE_V4_EGRESS = "true"'
        )

    def test_prefix_delegation_enabled(self, eks_main_tf):
        # Mandatory for IPv6 mode — each node gets a /80 IPv6 prefix.
        block = _extract_resource_block(eks_main_tf, "aws_eks_addon", "vpc_cni")
        assert re.search(r'ENABLE_PREFIX_DELEGATION\s*=\s*"true"', block)


class TestBaseLaunchTemplateIPv6IMDS:
    """Base node launch template metadata_options must enable IPv6 IMDS so
    nodes can reach the instance metadata service over IPv6 (parity with
    Karpenter EC2NodeClass templates that set httpProtocolIPv6: enabled)."""

    def test_metadata_options_enable_ipv6(self, eks_main_tf):
        block = _extract_resource_block(eks_main_tf, "aws_launch_template", "base")
        assert re.search(r'http_protocol_ipv6\s*=\s*"enabled"', block), (
            'aws_launch_template.base.metadata_options must set http_protocol_ipv6 = "enabled"'
        )


class TestBaseLaunchTemplateServiceCIDRPrecondition:
    """Base launch template must precondition on a non-empty service_ipv6_cidr.

    The user-data template consumes service_ipv6_cidr; if it's empty the node
    boots with a broken kubelet config. Failing fast in apply is far easier to
    diagnose than tracing a broken DNS lookup back to an empty CIDR."""

    def test_lifecycle_precondition_on_service_ipv6_cidr(self, eks_main_tf):
        block = _extract_resource_block(eks_main_tf, "aws_launch_template", "base")
        assert "lifecycle" in block, "aws_launch_template.base must have a lifecycle block"
        assert "precondition" in block, "aws_launch_template.base.lifecycle must have a precondition"
        # The precondition must reference the EKS cluster's service_ipv6_cidr
        # and must be a non-empty check.
        assert re.search(
            r"aws_eks_cluster\.this\.kubernetes_network_config\[0\]\.service_ipv6_cidr\s*!=\s*\"\"",
            block,
        ), 'lifecycle.precondition must check service_ipv6_cidr != ""'


class TestNodeCNIIPv6IAMPolicy:
    """aws_iam_role_policy.node_cni_ipv6 follows the canonical AWS IPv6 IAM
    doc: AssignIpv6Addresses + Describe* + CreateTags. UnassignIpv6Addresses
    is intentionally omitted (not in the AWS doc; implicit on ENI detach).

    Reference: https://docs.aws.amazon.com/eks/latest/userguide/cni-iam-role.html
    """

    def test_assign_ipv6_addresses_present(self, eks_main_tf):
        block = _extract_resource_block(eks_main_tf, "aws_iam_role_policy", "node_cni_ipv6")
        assert '"ec2:AssignIpv6Addresses"' in block

    def test_unassign_ipv6_addresses_omitted(self, eks_main_tf):
        block = _extract_resource_block(eks_main_tf, "aws_iam_role_policy", "node_cni_ipv6")
        assert "UnassignIpv6Addresses" not in block, (
            "ec2:UnassignIpv6Addresses is not in the AWS canonical IPv6 IAM doc — drop it from the policy"
        )

    def test_create_tags_on_eni(self, eks_main_tf):
        block = _extract_resource_block(eks_main_tf, "aws_iam_role_policy", "node_cni_ipv6")
        assert '"ec2:CreateTags"' in block
        assert "arn:aws:ec2:*:*:network-interface/*" in block, (
            "ec2:CreateTags must be scoped to network-interface ARNs only"
        )
