"""Unit tests for the per-(bucket, AZ) NAT Gateway / EIP / route table topology in modules/eks/terraform/modules/vpc/nat.tf.

Each NAT GW gets 1 primary EIP plus N-1 secondary EIPs (configurable via
`var.nat_gateway_eip_count`, default 8, AWS hard cap 8).

Topology end state (production: 4 buckets x 3 AZs):
- 12 NAT GWs (one per bucket-AZ pair)
- 12 dedicated pod route tables (one per bucket-AZ pair)
- 96 EIPs at default `nat_gateway_eip_count = 8`
- 3 per-AZ private route tables for nodes (route through bucket-1's NAT GW per AZ)

Tests parse the .tf source as plain text + regex (no HCL parser available;
matches the established convention in scripts/test_vpc_subnet_tags.py and
scripts/test_vpc_cni_addon.py). Tests are intentionally fragile to silent
refactors (e.g. hoisting a value into `local.x`) so test failures surface
the change explicitly.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
VPC_DIR = REPO_ROOT / "modules" / "eks" / "terraform" / "modules" / "vpc"
VPC_NAT_TF = VPC_DIR / "nat.tf"
VPC_MAIN_TF = VPC_DIR / "main.tf"
VPC_VARS_TF = VPC_DIR / "variables.tf"
ROOT_VARS_TF = REPO_ROOT / "modules" / "eks" / "terraform" / "variables.tf"

VPC_NAT_TF_TEXT = VPC_NAT_TF.read_text() if VPC_NAT_TF.exists() else ""
VPC_MAIN_TF_TEXT = VPC_MAIN_TF.read_text()
VPC_VARS_TF_TEXT = VPC_VARS_TF.read_text()
ROOT_VARS_TF_TEXT = ROOT_VARS_TF.read_text()

# Make sibling helper module importable regardless of pytest invocation cwd.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _tf_parse_helpers import (  # noqa: E402
    resource_block,
    strip_double_quoted_strings,
)


def _strip_hcl_comments(text: str) -> str:
    """Remove `# ...` and `// ...` line comments and `/* ... */` block comments.

    Strings are scrubbed first so a `#` inside a quoted string doesn't trigger
    comment-stripping mid-literal.
    """
    scrubbed = strip_double_quoted_strings(text)
    # Block comments first
    scrubbed = re.sub(r"/\*.*?\*/", "", scrubbed, flags=re.DOTALL)
    # Line comments
    scrubbed = re.sub(r"#[^\n]*", "", scrubbed)
    scrubbed = re.sub(r"//[^\n]*", "", scrubbed)
    return scrubbed


def _variable_block(text: str, name: str) -> str:
    """Return the brace-balanced body of a top-level
    `variable "<name>" { ... }` block (including the outer braces).

    Local helper -- not added to _tf_parse_helpers because no other test
    currently parses variable blocks.
    """
    pattern = re.compile(
        rf'variable\s+"{re.escape(name)}"\s*\{{',
        re.MULTILINE,
    )
    m = pattern.search(text)
    assert m, f'variable "{name}" not found'
    start = m.end() - 1  # position of opening brace
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    raise AssertionError(f"unterminated block for variable {name}")


def _tags_block_text(block: str) -> str:
    """Extract the merge(...) call inside a `tags = merge(` assignment.

    Returns the paren-balanced contents from `merge(` to its closing `)`.
    Used to scope tag-content searches to the actual tag map and exclude
    references elsewhere in the resource block (e.g. inside a precondition
    error_message).
    """
    m = re.search(r"tags\s*=\s*merge\(", block)
    assert m, "no tags = merge(...) found"
    start = m.end()  # position right after the opening (
    depth = 1
    for i in range(start, len(block)):
        if block[i] == "(":
            depth += 1
        elif block[i] == ")":
            depth -= 1
            if depth == 0:
                return block[start:i]
    raise AssertionError("unterminated merge(...) block")


def _dynamic_route_block(block: str) -> str:
    """Extract the brace-balanced body of `dynamic "route" { ... }` (including outer braces)."""
    m = re.search(r'dynamic\s+"route"\s*\{', block)
    assert m, 'no dynamic "route" { ... } block found'
    start = m.end() - 1
    depth = 0
    for i in range(start, len(block)):
        if block[i] == "{":
            depth += 1
        elif block[i] == "}":
            depth -= 1
            if depth == 0:
                return block[start : i + 1]
    raise AssertionError('unterminated dynamic "route" block')


class TestNATFileExists:
    """nat.tf must exist at the canonical path and declare all 7 NAT-topology resources."""

    def test_nat_tf_file_exists(self) -> None:
        assert VPC_NAT_TF.exists(), (
            f"nat.tf must exist at {VPC_NAT_TF.relative_to(REPO_ROOT)} -- the VPC NAT topology "
            "(EIPs, NAT GWs, pod route tables, refactored private route tables) lives in this file."
        )

    def test_expected_resource_declarations_present(self) -> None:
        """Count distinct top-level resources in nat.tf -- must contain all 7
        per-(bucket, AZ) NAT topology resources, no more, no less.
        """
        # All resource declarations matching the NAT topology types
        decls = re.findall(
            r'^resource\s+"(aws_(?:eip|nat_gateway|route_table|route_table_association))"\s+"([a-z_]+)"',
            VPC_NAT_TF_TEXT,
            re.MULTILINE,
        )
        decl_pairs = sorted(decls)
        expected = sorted(
            [
                ("aws_eip", "nat_primary"),
                ("aws_eip", "nat_secondary"),
                ("aws_nat_gateway", "this"),
                ("aws_route_table", "pod"),
                ("aws_route_table", "private"),
                ("aws_route_table_association", "pod"),
                ("aws_route_table_association", "private"),
            ]
        )
        assert decl_pairs == expected, f"nat.tf must declare exactly these 7 resources: {expected}. Got {decl_pairs}."


class TestNATGatewayResource:
    """aws_nat_gateway.this is the per-(bucket, AZ) NAT Gateway.

    Topology contract:
    - Keyed by `local.pod_cidr_associations` ("${bucket}-${az}")
    - Gated by `var.enable_nat_gateway` to preserve the off-switch
    - Primary allocation_id from aws_eip.nat_primary[each.key]
    - Secondary EIPs via for slot in range(2, var.nat_gateway_eip_count + 1)
    - Subnet placement via local.public_subnets_by_az[each.value.az]
    - osdc.io/nat-{bucket,az} tags
    - lifecycle.precondition asserting pod_azs == var.azs (defends AZ drift)
    """

    block = resource_block(VPC_NAT_TF_TEXT, "aws_nat_gateway", "this") if VPC_NAT_TF_TEXT else ""

    def test_for_each_uses_pod_cidr_associations(self) -> None:
        # Match the literal `local.pod_cidr_associations` after the colon -- the
        # gating ternary `var.enable_nat_gateway ? local.pod_cidr_associations : {}`.
        assert re.search(
            r"for_each\s*=\s*var\.enable_nat_gateway\s*\?\s*local\.pod_cidr_associations\s*:", self.block
        ), (
            "aws_nat_gateway.this for_each must be `var.enable_nat_gateway ? local.pod_cidr_associations : {}` "
            "so NAT GWs are keyed per (bucket, AZ) and the off-switch is preserved."
        )

    def test_for_each_off_switch_returns_empty_map(self) -> None:
        assert re.search(
            r"for_each\s*=\s*var\.enable_nat_gateway\s*\?\s*local\.pod_cidr_associations\s*:\s*\{\s*\}", self.block
        ), (
            "aws_nat_gateway.this for_each must fall back to `{}` (empty map) when "
            "`var.enable_nat_gateway` is false. This preserves the off-switch."
        )

    def test_allocation_id_references_nat_primary(self) -> None:
        assert re.search(r"allocation_id\s*=\s*aws_eip\.nat_primary\[each\.key\]\.id", self.block), (
            "aws_nat_gateway.this allocation_id must be `aws_eip.nat_primary[each.key].id` -- "
            "each NAT GW gets exactly one primary EIP keyed by the same bucket-az key."
        )

    def test_secondary_allocation_ids_uses_eip_count_range(self) -> None:
        assert re.search(
            r"secondary_allocation_ids\s*=\s*\[\s*for\s+slot\s+in\s+range\(\s*2\s*,\s*var\.nat_gateway_eip_count\s*\+\s*1\s*\)",
            self.block,
        ), (
            "aws_nat_gateway.this secondary_allocation_ids must use "
            "`[for slot in range(2, var.nat_gateway_eip_count + 1) : ...]` -- this is how "
            "the variable count drives EIP attachment. Slot starts at 2 because slot 1 is the primary."
        )

    def test_secondary_allocation_ids_references_nat_secondary(self) -> None:
        assert re.search(r"aws_eip\.nat_secondary\[", self.block), (
            "aws_nat_gateway.this secondary_allocation_ids must reference `aws_eip.nat_secondary[...]` "
            "(keyed by '${bucket}-${az}-${slot}'). Without this, the secondary EIPs are dangling."
        )

    def test_subnet_id_uses_public_subnets_by_az(self) -> None:
        assert re.search(r"subnet_id\s*=\s*local\.public_subnets_by_az\[each\.value\.az\]", self.block), (
            "aws_nat_gateway.this subnet_id must be `local.public_subnets_by_az[each.value.az]` -- "
            "NAT GWs live in the public subnet for that AZ. Using `var.azs[i]` is positional and "
            "vulnerable to AZ-list drift."
        )

    def test_tags_include_name_and_nat_metadata(self) -> None:
        tags = _tags_block_text(self.block)
        assert "Name" in tags, "aws_nat_gateway.this tags must include Name"
        assert "osdc.io/nat-bucket" in tags, (
            "aws_nat_gateway.this tags must include osdc.io/nat-bucket -- needed by the smoke "
            "tests to verify per-(bucket, AZ) topology."
        )
        assert "osdc.io/nat-az" in tags, (
            "aws_nat_gateway.this tags must include osdc.io/nat-az -- needed by the smoke "
            "tests to verify per-(bucket, AZ) topology."
        )

    def test_lifecycle_precondition_asserts_pod_azs_eq_var_azs(self) -> None:
        assert "lifecycle" in self.block, "aws_nat_gateway.this must declare a lifecycle block with a precondition."
        assert "precondition" in self.block, (
            "aws_nat_gateway.this lifecycle must contain a precondition asserting "
            "`sort(local.pod_azs) == sort(var.azs)`. Without it, an AZ mismatch between "
            "pod_cidr_buckets and var.azs would silently misplace NAT GWs."
        )
        assert re.search(r"sort\(\s*local\.pod_azs\s*\)\s*==\s*sort\(\s*var\.azs\s*\)", self.block), (
            "aws_nat_gateway.this precondition must assert `sort(local.pod_azs) == sort(var.azs)` "
            "(both sorted, both compared by ==). The sort is required because the lists have no "
            "guaranteed order."
        )

    def test_depends_on_internet_gateway(self) -> None:
        assert re.search(r"depends_on\s*=\s*\[[^\]]*aws_internet_gateway\.this", self.block), (
            "aws_nat_gateway.this must depends_on aws_internet_gateway.this -- AWS rejects NAT GW "
            "creation if the IGW isn't attached yet."
        )


class TestEIPPrimaryResource:
    """aws_eip.nat_primary -- exactly one primary EIP per (bucket, AZ) NAT GW."""

    block = resource_block(VPC_NAT_TF_TEXT, "aws_eip", "nat_primary") if VPC_NAT_TF_TEXT else ""

    def test_for_each_pod_cidr_associations_gated(self) -> None:
        assert re.search(
            r"for_each\s*=\s*var\.enable_nat_gateway\s*\?\s*local\.pod_cidr_associations\s*:\s*\{\s*\}",
            self.block,
        ), (
            "aws_eip.nat_primary for_each must be "
            "`var.enable_nat_gateway ? local.pod_cidr_associations : {}` -- one primary EIP per "
            "bucket-AZ NAT GW, off-switch preserved."
        )

    def test_domain_is_vpc(self) -> None:
        assert re.search(r'domain\s*=\s*"vpc"', self.block), (
            'aws_eip.nat_primary must declare `domain = "vpc"` (post-EC2-Classic-deprecation API).'
        )

    def test_tags_include_role_primary(self) -> None:
        tags = _tags_block_text(self.block)
        assert re.search(r'"osdc\.io/nat-eip-role"\s*=\s*"primary"', tags), (
            'aws_eip.nat_primary tags must include `"osdc.io/nat-eip-role" = "primary"` -- '
            "smoke tests filter EIPs by role to verify 1-primary-per-NAT-GW topology."
        )

    def test_tags_include_bucket_and_az(self) -> None:
        tags = _tags_block_text(self.block)
        # Bucket / AZ tag keys may use the same prefix as the NAT GW resource
        # (osdc.io/nat-*). Accept either `osdc.io/nat-bucket` (consistency with
        # aws_nat_gateway.this) or `osdc.io/nat-eip-bucket`.
        assert ("osdc.io/nat-bucket" in tags) or ("osdc.io/nat-eip-bucket" in tags), (
            "aws_eip.nat_primary tags must include a bucket tag (osdc.io/nat-bucket or "
            "osdc.io/nat-eip-bucket) so EIPs are filterable per bucket."
        )
        assert ("osdc.io/nat-az" in tags) or ("osdc.io/nat-eip-az" in tags), (
            "aws_eip.nat_primary tags must include an AZ tag (osdc.io/nat-az or "
            "osdc.io/nat-eip-az) so EIPs are filterable per AZ."
        )

    def test_depends_on_internet_gateway(self) -> None:
        assert re.search(r"depends_on\s*=\s*\[[^\]]*aws_internet_gateway\.this", self.block), (
            "aws_eip.nat_primary must depends_on aws_internet_gateway.this so EIP allocation "
            "ordering matches the NAT GW (which also depends on the IGW)."
        )


class TestEIPSecondaryResource:
    """aws_eip.nat_secondary -- slots 2..N per (bucket, AZ) NAT GW.

    Empty map when nat_gateway_eip_count == 1. Each EIP keyed
    "${bucket}-${az}-${slot}".
    """

    block = resource_block(VPC_NAT_TF_TEXT, "aws_eip", "nat_secondary") if VPC_NAT_TF_TEXT else ""

    def test_for_each_nat_secondary_assignments_gated(self) -> None:
        assert re.search(
            r"for_each\s*=\s*var\.enable_nat_gateway\s*\?\s*local\.nat_secondary_assignments\s*:\s*\{\s*\}",
            self.block,
        ), (
            "aws_eip.nat_secondary for_each must be "
            "`var.enable_nat_gateway ? local.nat_secondary_assignments : {}` -- one secondary "
            "EIP per slot in 2..nat_gateway_eip_count, off-switch preserved."
        )

    def test_tags_include_role_secondary(self) -> None:
        tags = _tags_block_text(self.block)
        assert re.search(r'"osdc\.io/nat-eip-role"\s*=\s*"secondary"', tags), (
            'aws_eip.nat_secondary tags must include `"osdc.io/nat-eip-role" = "secondary"` -- '
            "smoke tests filter EIPs by role to verify N-1 secondary EIPs per NAT GW."
        )

    def test_tags_include_slot(self) -> None:
        tags = _tags_block_text(self.block)
        assert "osdc.io/nat-eip-slot" in tags, (
            "aws_eip.nat_secondary tags must include osdc.io/nat-eip-slot -- needed to "
            "disambiguate which secondary slot (2..N) this EIP fills."
        )


class TestPodRouteTableResource:
    """aws_route_table.pod -- one route table per (bucket, AZ) pod subnet.

    Always created (NOT gated by enable_nat_gateway) because aws_subnet.pod
    always exists and route_table_association requires a target. The internet
    egress route is dynamic and only created when enable_nat_gateway is true.
    """

    block = resource_block(VPC_NAT_TF_TEXT, "aws_route_table", "pod") if VPC_NAT_TF_TEXT else ""

    def test_for_each_is_unconditional_pod_cidr_associations(self) -> None:
        # Match the literal for_each line and confirm it does NOT include the
        # var.enable_nat_gateway ternary -- pod RTs always exist.
        m = re.search(r"for_each\s*=\s*([^\n]+)", self.block)
        assert m, "aws_route_table.pod must declare a for_each"
        for_each_rhs = m.group(1).strip()
        assert "local.pod_cidr_associations" in for_each_rhs, (
            f"aws_route_table.pod for_each must reference `local.pod_cidr_associations` so the "
            f"route table exists per (bucket, AZ). Got `{for_each_rhs}`."
        )
        assert "var.enable_nat_gateway" not in for_each_rhs, (
            "aws_route_table.pod for_each must NOT be gated by var.enable_nat_gateway -- the "
            "pod route table is needed for aws_route_table_association.pod even when NAT is "
            "disabled. Gate the dynamic route block instead, not the table itself."
        )

    def test_dynamic_route_block_gated_by_enable_nat_gateway(self) -> None:
        dyn = _dynamic_route_block(self.block)
        assert re.search(r"for_each\s*=\s*var\.enable_nat_gateway\s*\?\s*\[\s*1\s*\]\s*:\s*\[\s*\]", dyn), (
            'aws_route_table.pod dynamic "route" must be gated by '
            "`for_each = var.enable_nat_gateway ? [1] : []` -- the route is only meaningful when "
            "NAT GWs exist. The off-switch removes the route while preserving the table."
        )

    def test_dynamic_route_targets_per_key_nat_gateway(self) -> None:
        dyn = _dynamic_route_block(self.block)
        assert re.search(r"nat_gateway_id\s*=\s*aws_nat_gateway\.this\[each\.key\]\.id", dyn), (
            'aws_route_table.pod dynamic "route" must set '
            "`nat_gateway_id = aws_nat_gateway.this[each.key].id` -- pod traffic egresses through "
            "the NAT GW for the SAME (bucket, AZ) pair as the pod subnet."
        )

    def test_tags_include_pod_route_metadata(self) -> None:
        tags = _tags_block_text(self.block)
        assert "osdc.io/pod-route-bucket" in tags, (
            "aws_route_table.pod tags must include osdc.io/pod-route-bucket so the smoke "
            "tests can verify per-(bucket, AZ) routing."
        )
        assert "osdc.io/pod-route-az" in tags, (
            "aws_route_table.pod tags must include osdc.io/pod-route-az so the smoke "
            "tests can verify per-(bucket, AZ) routing."
        )


class TestPodRouteTableAssociation:
    """aws_route_table_association.pod -- one association per (bucket, AZ) pod subnet.

    Maps each aws_subnet.pod[k] to its matching aws_route_table.pod[k].
    """

    block = resource_block(VPC_NAT_TF_TEXT, "aws_route_table_association", "pod") if VPC_NAT_TF_TEXT else ""

    def test_for_each_pod_cidr_associations(self) -> None:
        m = re.search(r"for_each\s*=\s*([^\n]+)", self.block)
        assert m, "aws_route_table_association.pod must declare a for_each"
        rhs = m.group(1).strip()
        assert "local.pod_cidr_associations" in rhs, (
            f"aws_route_table_association.pod for_each must reference `local.pod_cidr_associations` "
            f"so each pod subnet is associated to its matching pod RT. Got `{rhs}`."
        )

    def test_subnet_id_references_pod_subnet(self) -> None:
        assert re.search(r"subnet_id\s*=\s*aws_subnet\.pod\[each\.key\]\.id", self.block), (
            "aws_route_table_association.pod subnet_id must be `aws_subnet.pod[each.key].id` -- "
            "the same key drives both pod subnet and pod RT, ensuring 1:1 mapping per (bucket, AZ)."
        )

    def test_route_table_id_references_pod_rt(self) -> None:
        assert re.search(r"route_table_id\s*=\s*aws_route_table\.pod\[each\.key\]\.id", self.block), (
            "aws_route_table_association.pod route_table_id must be "
            "`aws_route_table.pod[each.key].id` -- same key as the subnet, enforcing 1:1 mapping."
        )


class TestPrivateRouteTable:
    """aws_route_table.private — one private RT per AZ (for_each over var.azs).

    Each AZ's RT routes 0.0.0.0/0 through the default_node_egress_bucket's NAT GW for that AZ.
    """

    block = resource_block(VPC_NAT_TF_TEXT, "aws_route_table", "private") if VPC_NAT_TF_TEXT else ""

    def test_for_each_uses_toset_var_azs(self) -> None:
        assert re.search(r"for_each\s*=\s*toset\(\s*var\.azs\s*\)", self.block), (
            "aws_route_table.private for_each must be `toset(var.azs)` -- one private RT per AZ, "
            "keyed by AZ name (NOT by count.index, which is index-shift fragile)."
        )

    def test_no_count_index_used(self) -> None:
        # Strip strings + comments before scanning so a comment or error_message
        # mentioning count.index doesn't false-positive.
        scrubbed = _strip_hcl_comments(self.block)
        assert "count.index" not in scrubbed, (
            "aws_route_table.private must use for_each = toset(var.azs), not count. count-indexed "
            "RTs cause destroy+recreate churn on AZ-list changes."
        )

    def test_no_count_attribute_at_top_level(self) -> None:
        # `count = ...` at top level of the resource block is forbidden.
        # Search for `count` as a field assignment (not preceded by var. or local. etc.).
        scrubbed = _strip_hcl_comments(self.block)
        assert not re.search(r"^\s*count\s*=", scrubbed, re.MULTILINE), (
            "aws_route_table.private must use for_each = toset(var.azs), not a top-level count."
        )

    def test_dynamic_route_block_gated_by_enable_nat_gateway(self) -> None:
        dyn = _dynamic_route_block(self.block)
        assert re.search(r"for_each\s*=\s*var\.enable_nat_gateway\s*\?\s*\[\s*1\s*\]\s*:\s*\[\s*\]", dyn), (
            'aws_route_table.private dynamic "route" must be gated by '
            "`for_each = var.enable_nat_gateway ? [1] : []` -- preserves the off-switch."
        )

    def test_nat_gateway_id_references_default_node_egress_bucket(self) -> None:
        dyn = _dynamic_route_block(self.block)
        assert "local.default_node_egress_bucket" in dyn, (
            'aws_route_table.private dynamic "route" must reference '
            "`local.default_node_egress_bucket` to compose the NAT GW key. This pins node egress "
            "through bucket-1's NAT GW per AZ (the operator-sorted-first bucket)."
        )
        assert "each.value" in dyn, (
            'aws_route_table.private dynamic "route" must use `each.value` to compose the NAT GW '
            "key with the AZ portion (each.value is the AZ string from toset(var.azs))."
        )
        assert re.search(r"aws_nat_gateway\.this\[", dyn), (
            'aws_route_table.private dynamic "route" must reference `aws_nat_gateway.this[...]` '
            "(the per-(bucket, AZ) NAT GW map)."
        )

    def test_no_single_nat_gateway_reference(self) -> None:
        scrubbed = _strip_hcl_comments(self.block)
        assert "var.single_nat_gateway" not in scrubbed, (
            "aws_route_table.private must NOT reference var.single_nat_gateway — that variable does not exist on this module."
        )
        assert "single_nat_gateway" not in scrubbed, (
            "aws_route_table.private must NOT reference single_nat_gateway in any form."
        )


class TestNoSingleNATGatewayReferences:
    """`var.single_nat_gateway` and the bare `single_nat_gateway` token must
    not appear in the VPC submodule (variable, references, or tag/field
    assignments). Comments are stripped before scanning so explanatory text
    doesn't interfere.
    """

    def _scrub(self, text: str) -> str:
        return _strip_hcl_comments(text)

    def test_no_single_nat_gateway_variable_in_vpc_variables(self) -> None:
        scrubbed = self._scrub(VPC_VARS_TF_TEXT)
        assert "single_nat_gateway" not in scrubbed, (
            "single_nat_gateway must not appear in vpc/variables.tf — that variable does not exist on this module."
        )

    def test_no_reference_in_vpc_main(self) -> None:
        scrubbed = self._scrub(VPC_MAIN_TF_TEXT)
        assert "single_nat_gateway" not in scrubbed, "single_nat_gateway must not appear in vpc/main.tf."

    def test_no_reference_in_vpc_nat(self) -> None:
        if not VPC_NAT_TF_TEXT:
            # Surface the underlying issue (file missing) via TestNATFileExists,
            # not via a misleading "no references" pass here.
            return
        scrubbed = self._scrub(VPC_NAT_TF_TEXT)
        assert "single_nat_gateway" not in scrubbed, "single_nat_gateway must not appear in vpc/nat.tf."


class TestNATGatewayEIPCountValidation:
    """`var.nat_gateway_eip_count` must be declared in BOTH the VPC submodule
    and the EKS root with the same shape: number, default 8, validation 1-8.
    """

    def _assert_variable_shape(self, text: str, file_label: str) -> None:
        block = _variable_block(text, "nat_gateway_eip_count")
        assert re.search(r"type\s*=\s*number", block), (
            f"{file_label}:variable nat_gateway_eip_count must declare `type = number`."
        )
        assert re.search(r"default\s*=\s*8\b", block), (
            f"{file_label}:variable nat_gateway_eip_count must declare `default = 8` (AWS hard cap)."
        )
        assert "validation" in block, (
            f"{file_label}:variable nat_gateway_eip_count must declare a validation block enforcing the 1-8 range."
        )
        # Condition must include both bounds. Match either `>= 1 && <= 8` or
        # the symmetric `<= 8 && >= 1`. Whitespace tolerant.
        cond_a = re.search(r">=\s*1[^&]*&&[^<]*<=\s*8", block)
        cond_b = re.search(r"<=\s*8[^&]*&&[^>]*>=\s*1", block)
        assert cond_a or cond_b, (
            f"{file_label}:variable nat_gateway_eip_count validation must enforce "
            "var.nat_gateway_eip_count >= 1 && var.nat_gateway_eip_count <= 8."
        )

    def test_vpc_submodule_variable_shape(self) -> None:
        if "nat_gateway_eip_count" not in VPC_VARS_TF_TEXT:
            raise AssertionError(
                "modules/eks/terraform/modules/vpc/variables.tf is missing variable "
                "'nat_gateway_eip_count' (number, default 8, validation 1-8)."
            )
        self._assert_variable_shape(VPC_VARS_TF_TEXT, "vpc/variables.tf")

    def test_root_variable_shape(self) -> None:
        if "nat_gateway_eip_count" not in ROOT_VARS_TF_TEXT:
            raise AssertionError(
                "modules/eks/terraform/variables.tf is missing variable 'nat_gateway_eip_count' "
                "(number, default 8, validation 1-8); cluster-config.py emits "
                "-var=nat_gateway_eip_count=N against this declaration."
            )
        self._assert_variable_shape(ROOT_VARS_TF_TEXT, "root variables.tf")
