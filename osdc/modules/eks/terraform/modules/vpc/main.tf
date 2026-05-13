terraform {
  required_version = ">= 1.7"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.23"
    }
  }
}

# VPC
resource "aws_vpc" "this" {
  cidr_block           = var.cidr
  enable_dns_hostnames = var.enable_dns_hostnames
  enable_dns_support   = var.enable_dns_support

  tags = merge(
    var.tags,
    {
      Name = var.name
    }
  )
}

# Pod CIDR associations -- secondary /16 blocks attached to the VPC for VPC CNI
# Custom Networking. One /16 per (bucket, AZ) keyed by "${bucket}-${az}". Pure
# additive -- no subnets carved here (PR 5) and no traffic until Custom
# Networking is enabled (PR 7).
locals {
  pod_cidr_associations = merge([
    for bucket_name, az_map in var.pod_cidr_buckets : {
      for az_name, cidr in az_map :
      "${bucket_name}-${az_name}" => {
        bucket = bucket_name
        az     = az_name
        cidr   = cidr
      }
    }
  ]...)
}

resource "aws_vpc_ipv4_cidr_block_association" "pod" {
  for_each = local.pod_cidr_associations

  vpc_id     = aws_vpc.this.id
  cidr_block = each.value.cidr

  lifecycle {
    precondition {
      condition     = contains(var.azs, each.value.az)
      error_message = "pod_cidr_buckets uses AZ '${each.value.az}' for bucket '${each.value.bucket}' but the cluster's available AZs are: ${join(", ", var.azs)}. Fix the AZ key in clusters.yaml or check that the AWS region actually has that AZ."
    }
  }
}

# Pod subnets -- one /16 subnet per (bucket, AZ). Each subnet IS the entire /16
# from the matching aws_vpc_ipv4_cidr_block_association.pod -- AWS caps VPC
# CIDRs at /16, so no further carving is possible. Pod subnets carry
# osdc.io/pod-subnet-* tags ONLY -- they MUST NOT carry karpenter.sh/discovery
# or kubernetes.io/role/{internal-elb,elb}. Karpenter (which discovers via
# karpenter.sh/discovery) only sees private_subnet_ids; pod subnets are
# exposed via a separate pod_subnet_ids output. Subnet/tag boundary is
# enforced by the unit test in scripts/test_vpc_subnet_tags.py.
resource "aws_subnet" "pod" {
  for_each = local.pod_cidr_associations

  vpc_id            = aws_vpc.this.id
  cidr_block        = each.value.cidr
  availability_zone = each.value.az

  tags = merge(
    var.tags,
    {
      Name                        = "${var.name}-pod-${each.value.bucket}-${each.value.az}"
      "osdc.io/pod-subnet-bucket" = each.value.bucket
      "osdc.io/pod-subnet-az"     = each.value.az
    }
  )

  lifecycle {
    # Defense-in-depth: ensure no caller injects subnet-discovery tags via var.tags.
    # Pod subnets MUST NOT carry karpenter.sh/discovery (would make Karpenter land
    # nodes on CGNAT pod CIDRs) or kubernetes.io/role/{internal-elb,elb} (would make
    # AWS load balancers land on pod CIDRs). The unit test inspects this resource
    # block source only -- this precondition catches injection at the var.tags
    # boundary that the test cannot see. See INCREASE_IPV4.md PR 5.
    precondition {
      condition = !anytrue([
        contains(keys(var.tags), "karpenter.sh/discovery"),
        contains(keys(var.tags), "kubernetes.io/role/internal-elb"),
        contains(keys(var.tags), "kubernetes.io/role/elb"),
      ])
      error_message = "var.tags MUST NOT contain karpenter.sh/discovery, kubernetes.io/role/internal-elb, or kubernetes.io/role/elb -- they would propagate to aws_subnet.pod and break the pod-vs-node-subnet boundary. See INCREASE_IPV4.md PR 5."
    }
  }

  depends_on = [aws_vpc_ipv4_cidr_block_association.pod]
}

# Internet Gateway
resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id

  tags = merge(
    var.tags,
    {
      Name = var.name
    }
  )
}

# Public Subnets
resource "aws_subnet" "public" {
  count = length(var.public_subnets)

  vpc_id                  = aws_vpc.this.id
  cidr_block              = var.public_subnets[count.index]
  availability_zone       = var.azs[count.index]
  map_public_ip_on_launch = true

  tags = merge(
    var.tags,
    {
      Name                     = "${var.name}-public-${var.azs[count.index]}"
      "kubernetes.io/role/elb" = "1"
    }
  )

  # Force sequential creation to avoid AWS eventual consistency issues
  depends_on = [aws_vpc.this, aws_internet_gateway.this]
}

# Private Subnets
resource "aws_subnet" "private" {
  count = length(var.private_subnets)

  vpc_id            = aws_vpc.this.id
  cidr_block        = var.private_subnets[count.index]
  availability_zone = var.azs[count.index]

  tags = merge(
    var.tags,
    var.private_subnet_tags,
    {
      Name                              = "${var.name}-private-${var.azs[count.index]}"
      "kubernetes.io/role/internal-elb" = "1"
    }
  )

  lifecycle {
    ignore_changes = [
      tags["karpenter.sh/discovery"],
    ]
  }

  # Force sequential creation to avoid AWS eventual consistency issues
  depends_on = [aws_vpc.this, aws_internet_gateway.this]
}

# AWS Subnet CIDR Reservations for VPC CNI Prefix Delegation.
# Reserves the top /23 (512 IPs / 32 prefix slots) of each primary /18
# private subnet. PD allocates /28 prefixes from inside the reservation
# first, falling back to the rest of the subnet only when reserved space
# is exhausted -- fragmentation protection for high pod-churn workloads.
# High-end indexing (cidrsubnet position 31 of 32) minimizes collision
# with existing low-numbered IP allocations.
locals {
  pd_prefix_reservations = {
    for idx, subnet in aws_subnet.private :
    subnet.availability_zone => {
      subnet_id  = subnet.id
      cidr_block = cidrsubnet(var.private_subnets[idx], 5, 31)
    }
  }
}

resource "aws_ec2_subnet_cidr_reservation" "pd_prefix" {
  for_each = local.pd_prefix_reservations

  cidr_block       = each.value.cidr_block
  reservation_type = "prefix"
  subnet_id        = each.value.subnet_id
  description      = "VPC CNI Prefix Delegation reservation (${each.key})"
}

# Elastic IPs for NAT Gateways
resource "aws_eip" "nat" {
  count  = var.enable_nat_gateway ? (var.single_nat_gateway ? 1 : length(var.azs)) : 0
  domain = "vpc"

  tags = merge(
    var.tags,
    {
      Name = "${var.name}-nat-${count.index + 1}"
    }
  )

  depends_on = [aws_internet_gateway.this]
}

# NAT Gateways
resource "aws_nat_gateway" "this" {
  count = var.enable_nat_gateway ? (var.single_nat_gateway ? 1 : length(var.azs)) : 0

  allocation_id = aws_eip.nat[count.index].id
  subnet_id     = aws_subnet.public[count.index].id

  tags = merge(
    var.tags,
    {
      Name = "${var.name}-nat-${count.index + 1}"
    }
  )

  depends_on = [aws_internet_gateway.this]
}

# Public Route Table
resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.this.id
  }

  tags = merge(
    var.tags,
    {
      Name = "${var.name}-public"
    }
  )
}

resource "aws_route_table_association" "public" {
  count = length(var.public_subnets)

  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

# Private Route Tables
resource "aws_route_table" "private" {
  count = var.enable_nat_gateway ? (var.single_nat_gateway ? 1 : length(var.azs)) : length(var.azs)

  vpc_id = aws_vpc.this.id

  dynamic "route" {
    for_each = var.enable_nat_gateway ? [1] : []
    content {
      cidr_block     = "0.0.0.0/0"
      nat_gateway_id = var.single_nat_gateway ? aws_nat_gateway.this[0].id : aws_nat_gateway.this[count.index].id
    }
  }

  tags = merge(
    var.tags,
    {
      Name = "${var.name}-private-${count.index + 1}"
    }
  )
}

resource "aws_route_table_association" "private" {
  count = length(var.private_subnets)

  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = var.single_nat_gateway ? aws_route_table.private[0].id : aws_route_table.private[count.index].id
}
