# Per-(bucket, AZ) NAT Gateway topology with configurable EIP count.
#
# Each (bucket, AZ) pod subnet gets its own NAT GW so pod egress source IPs
# are isolated per bucket. Each NAT GW gets var.nat_gateway_eip_count EIPs
# (1 primary + N-1 secondary, AWS hard cap of 8) for source-IP diversity to
# mitigate per-IP rate limits at upstream services.
#
# Production end state: 4 buckets x 3 AZs = 12 NAT GWs, 96 EIPs at default 8.
# Staging end state: 4 buckets x 2 AZs = 8 NAT GWs, 8 EIPs at override 1.
#
# Base node egress (private subnets, primary CIDR) routes through the
# default_node_egress_bucket NAT GW per AZ -- preserves a single egress
# source-IP set for base nodes regardless of bucket count.

locals {
  # AZ keys derived from pod_cidr_buckets, NOT var.azs. Pod-subnet topology is
  # the single source of truth for AZ placement; var.azs is positional and
  # AWS-API-discovered. Used only for the precondition cross-check below.
  pod_azs = sort(distinct(flatten([
    for bucket_name, az_map in var.pod_cidr_buckets : keys(az_map)
  ])))

  # The bucket whose per-AZ NAT GWs serve base-node (private subnet) egress.
  # Base nodes live in the primary CIDR; they need exactly one NAT GW per AZ
  # (not one per bucket per AZ). Lexicographically first bucket name is the
  # stable, deterministic choice.
  #
  # All base-node (private subnet) egress for a given AZ routes through THIS one
  # bucket's NAT GW. SPOF: if bucket-1's NAT GW fails in an AZ, ALL base nodes
  # in that AZ lose internet egress (Harbor pulls, Karpenter EC2 calls, ARC
  # heartbeats, Alloy -> Grafana Cloud). Acceptable trade-off vs adding 3+ extra
  # NAT GWs (~$100/mo overhead) just for base-node redundancy. Pod egress is
  # diversified per-bucket as designed.
  default_node_egress_bucket = sort(keys(var.pod_cidr_buckets))[0]

  # Flat map of secondary EIP slots keyed "${bucket}-${az}-${slot}" for slot
  # in 2..nat_gateway_eip_count. Empty map when nat_gateway_eip_count == 1.
  nat_secondary_assignments = merge([
    for nat_key, assoc in local.pod_cidr_associations : {
      for slot in range(2, var.nat_gateway_eip_count + 1) :
      "${nat_key}-${slot}" => {
        bucket  = assoc.bucket
        az      = assoc.az
        slot    = slot
        nat_key = nat_key
      }
    }
  ]...)

  # Subnet-by-AZ maps derived from the actual aws_subnet attribute, NOT
  # positional var.azs[i]. The existing private_subnets_by_az output uses
  # positional indexing; these locals are intentionally separate to avoid
  # drift if the count ordering ever diverges from var.azs.
  public_subnets_by_az = {
    for s in aws_subnet.public : s.availability_zone => s.id
  }

  private_subnets_by_az_local = {
    for s in aws_subnet.private : s.availability_zone => s.id
  }
}

# Primary EIP per NAT GW. Always exactly 1 per (bucket, AZ).
resource "aws_eip" "nat_primary" {
  for_each = var.enable_nat_gateway ? local.pod_cidr_associations : {}

  domain = "vpc"

  tags = merge(
    var.tags,
    {
      Name                   = "${var.name}-nat-${each.value.bucket}-${each.value.az}-primary"
      "osdc.io/nat-bucket"   = each.value.bucket
      "osdc.io/nat-az"       = each.value.az
      "osdc.io/nat-eip-role" = "primary"
    }
  )

  depends_on = [aws_internet_gateway.this]

  lifecycle {
    # Pod-subnet AZ keys MUST match the cluster's available AZs. If they drift,
    # NAT placement either fails (missing public subnet for the AZ) or worse,
    # silently picks the wrong AZ. Fail at plan time with a clear message.
    precondition {
      condition = sort(local.pod_azs) == sort(var.azs)
      error_message = format(
        "pod_cidr_buckets AZ keys (%s) must match var.azs (%s). Pod subnets and node subnets must live in the same AZs.",
        jsonencode(local.pod_azs),
        jsonencode(var.azs)
      )
    }
  }
}

# Secondary EIPs per NAT GW (slots 2..nat_gateway_eip_count). Empty when
# nat_gateway_eip_count == 1.
resource "aws_eip" "nat_secondary" {
  for_each = var.enable_nat_gateway ? local.nat_secondary_assignments : {}

  domain = "vpc"

  tags = merge(
    var.tags,
    {
      Name                   = "${var.name}-nat-${each.value.bucket}-${each.value.az}-secondary-${each.value.slot}"
      "osdc.io/nat-bucket"   = each.value.bucket
      "osdc.io/nat-az"       = each.value.az
      "osdc.io/nat-eip-role" = "secondary"
      "osdc.io/nat-eip-slot" = tostring(each.value.slot)
    }
  )

  depends_on = [aws_internet_gateway.this]
}

# NAT Gateway per (bucket, AZ). Allocation = 1 primary EIP + (N-1) secondary
# EIPs. Lives in the matching public subnet (looked up by AZ from the
# attribute-derived map -- avoids positional var.azs[i] drift).
resource "aws_nat_gateway" "this" {
  for_each = var.enable_nat_gateway ? local.pod_cidr_associations : {}

  allocation_id = aws_eip.nat_primary[each.key].id
  secondary_allocation_ids = [
    for slot in range(2, var.nat_gateway_eip_count + 1) :
    aws_eip.nat_secondary["${each.key}-${slot}"].id
  ]
  subnet_id = local.public_subnets_by_az[each.value.az]

  tags = merge(
    var.tags,
    {
      Name                 = "${var.name}-nat-${each.value.bucket}-${each.value.az}"
      "osdc.io/nat-bucket" = each.value.bucket
      "osdc.io/nat-az"     = each.value.az
    }
  )

  depends_on = [aws_internet_gateway.this]

  lifecycle {
    # Pod-subnet AZ keys MUST match the cluster's available AZs. If they drift,
    # NAT placement either fails (missing public subnet for the AZ) or worse,
    # silently picks the wrong AZ. Fail at plan time with a clear message.
    precondition {
      condition = sort(local.pod_azs) == sort(var.azs)
      error_message = format(
        "pod_cidr_buckets AZ keys (%s) must match var.azs (%s). Pod subnets and node subnets must live in the same AZs.",
        jsonencode(local.pod_azs),
        jsonencode(var.azs)
      )
    }
  }
}

# Pod route table per (bucket, AZ). Always created (NOT gated by
# enable_nat_gateway) so the pod subnet associations remain stable; the
# default route is gated dynamically and disappears when NAT is disabled.
# This preserves the off-switch without destroy/recreate churn on the route
# table itself.
resource "aws_route_table" "pod" {
  for_each = local.pod_cidr_associations

  vpc_id = aws_vpc.this.id

  dynamic "route" {
    for_each = var.enable_nat_gateway ? [1] : []
    content {
      cidr_block     = "0.0.0.0/0"
      nat_gateway_id = aws_nat_gateway.this[each.key].id
    }
  }

  tags = merge(
    var.tags,
    {
      Name                       = "${var.name}-pod-${each.value.bucket}-${each.value.az}"
      "osdc.io/pod-route-bucket" = each.value.bucket
      "osdc.io/pod-route-az"     = each.value.az
    }
  )
}

resource "aws_route_table_association" "pod" {
  for_each = local.pod_cidr_associations

  subnet_id      = aws_subnet.pod[each.key].id
  route_table_id = aws_route_table.pod[each.key].id
}

# Private (base node) route table per AZ. Routes through the
# default_node_egress_bucket's NAT GW for that AZ -- base nodes share a single
# egress source-IP set per AZ regardless of bucket count.
resource "aws_route_table" "private" {
  for_each = toset(var.azs)

  vpc_id = aws_vpc.this.id

  dynamic "route" {
    for_each = var.enable_nat_gateway ? [1] : []
    content {
      cidr_block     = "0.0.0.0/0"
      nat_gateway_id = aws_nat_gateway.this["${local.default_node_egress_bucket}-${each.value}"].id
    }
  }

  tags = merge(
    var.tags,
    {
      Name = "${var.name}-private-${each.value}"
    }
  )
}

resource "aws_route_table_association" "private" {
  for_each = local.private_subnets_by_az_local

  subnet_id      = each.value
  route_table_id = aws_route_table.private[each.key].id
}
