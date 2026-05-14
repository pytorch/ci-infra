"""Unit tests for VPC subnet tag mutual-exclusivity.

`aws_subnet.pod` exists alongside the existing `aws_subnet.private`.
The two MUST carry disjoint tag sets so Karpenter (which discovers via
`karpenter.sh/discovery` + `kubernetes.io/role/internal-elb`) only ever lands
nodes on the existing private subnets and never sees the new CGNAT pod
subnets.

Tests parse the .tf source as plain text + regex (no HCL parser available;
matches the established convention in test_cluster_config.py).
"""

import re
from pathlib import Path

VPC_MAIN_TF = Path(__file__).resolve().parent.parent / "modules" / "eks" / "terraform" / "modules" / "vpc" / "main.tf"
VPC_MAIN_TF_TEXT = VPC_MAIN_TF.read_text()


def _resource_block(text: str, resource_type: str, name: str) -> str:
    """Return the brace-balanced body of a top-level
    `resource "<type>" "<name>" { ... }` block (including the outer braces).
    """
    pattern = re.compile(
        rf'resource\s+"{re.escape(resource_type)}"\s+"{re.escape(name)}"\s*\{{',
        re.MULTILINE,
    )
    m = pattern.search(text)
    assert m, f'resource "{resource_type}" "{name}" not found in {VPC_MAIN_TF}'
    start = m.end() - 1  # position of opening brace
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    raise AssertionError(f"unterminated block for {resource_type}.{name}")


def _tags_block(text: str) -> str:
    """Extract the merge(...) call inside a `tags = merge(` assignment.

    Returns the paren-balanced contents from `merge(` to its closing `)`.
    Used to scope substring searches to the actual tag map and exclude
    other places (like a lifecycle.precondition error_message) where tag
    keys legitimately appear by name.
    """
    m = re.search(r"tags\s*=\s*merge\(", text)
    assert m, "no tags = merge(...) found"
    start = m.end()  # position right after the opening (
    depth = 1
    for i in range(start, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return text[start:i]
    raise AssertionError("unterminated merge(...) block")


class TestPodSubnetTags:
    """aws_subnet.pod MUST carry osdc.io/pod-subnet-* tags and MUST NOT carry
    node-subnet tags (karpenter.sh/discovery, kubernetes.io/role/*-elb).
    """

    block = _resource_block(VPC_MAIN_TF_TEXT, "aws_subnet", "pod")

    def test_pod_subnet_has_bucket_tag(self):
        assert "osdc.io/pod-subnet-bucket" in self.block, (
            "aws_subnet.pod missing required tag osdc.io/pod-subnet-bucket."
        )

    def test_pod_subnet_has_az_tag(self):
        assert "osdc.io/pod-subnet-az" in self.block, "aws_subnet.pod missing required tag osdc.io/pod-subnet-az."

    def test_pod_subnet_tag_block_has_no_karpenter_discovery(self):
        tags_block = _tags_block(self.block)
        assert "karpenter.sh/discovery" not in tags_block, (
            "aws_subnet.pod tag merge must NOT contain karpenter.sh/discovery -- "
            "Karpenter would land nodes on CGNAT pod subnets, breaking VPC CNI "
            "Custom Networking."
        )

    def test_pod_subnet_tag_block_has_no_internal_elb_role(self):
        tags_block = _tags_block(self.block)
        assert "kubernetes.io/role/internal-elb" not in tags_block, (
            "aws_subnet.pod tag merge must NOT contain kubernetes.io/role/internal-elb -- "
            "internal LBs would land in pod-IP space."
        )

    def test_pod_subnet_tag_block_has_no_elb_role(self):
        tags_block = _tags_block(self.block)
        assert "kubernetes.io/role/elb" not in tags_block, (
            "aws_subnet.pod tag merge must NOT contain kubernetes.io/role/elb -- "
            "external LBs would land in pod-IP space."
        )

    def test_pod_subnet_has_var_tags_precondition(self):
        """Regression guard: the var.tags injection precondition must remain on aws_subnet.pod."""
        assert "precondition" in self.block, (
            "aws_subnet.pod must have a lifecycle.precondition guarding against "
            "var.tags injection of forbidden tag keys."
        )
        for forbidden in (
            "karpenter.sh/discovery",
            "kubernetes.io/role/internal-elb",
            "kubernetes.io/role/elb",
        ):
            assert forbidden in self.block, (
                f"aws_subnet.pod precondition must reference {forbidden} so injection via var.tags fails at plan time."
            )


class TestPrivateSubnetTags:
    """aws_subnet.private MUST NOT carry pod-subnet tags (boundary inverse).

    Existing private-subnet tags (karpenter.sh/discovery, kubernetes.io/role/internal-elb)
    are out of scope and are NOT asserted here.
    """

    block = _resource_block(VPC_MAIN_TF_TEXT, "aws_subnet", "private")

    def test_private_subnet_has_no_pod_bucket_tag(self):
        assert "osdc.io/pod-subnet-bucket" not in self.block, (
            "aws_subnet.private must NOT carry osdc.io/pod-subnet-bucket -- that tag is reserved for aws_subnet.pod."
        )

    def test_private_subnet_has_no_pod_az_tag(self):
        assert "osdc.io/pod-subnet-az" not in self.block, (
            "aws_subnet.private must NOT carry osdc.io/pod-subnet-az -- that tag is reserved for aws_subnet.pod."
        )
