# Base infrastructure: VPC + EKS + Harbor S3/IAM
#
# This is cluster-agnostic. All cluster-specific values come from variables
# (passed via -var flags from justfile, which reads clusters.yaml).

data "aws_availability_zones" "available" {
  state = "available"

  # Exclude Local Zones and Wavelength Zones — they belong to separate network
  # border groups and cannot use the VPC's auto-assigned IPv6 CIDR block.
  filter {
    name   = "opt-in-status"
    values = ["opt-in-not-required"]
  }

  lifecycle {
    postcondition {
      condition     = alltrue([for id in local.configured_az_ids : contains(self.zone_ids, id)])
      error_message = "availability_zone_ids lists an AZ ID that is not an available, opt-in-not-required AZ in this region."
    }
  }
}

locals {
  # clusters.yaml base.availability_zones. See scripts/cluster-config.py.
  configured_az_ids = compact(split(",", var.availability_zone_ids))

  # AZ names differ between accounts for the same zone ID, so clusters.yaml
  # pins zone IDs. The VPC module still gets names: it keys the NAT secondary
  # EIPs by AZ name, and passing IDs would replace every one of them.
  az_name_by_id = zipmap(
    data.aws_availability_zones.available.zone_ids,
    data.aws_availability_zones.available.names
  )

  azs = length(local.configured_az_ids) > 0 ? [
    for id in local.configured_az_ids : local.az_name_by_id[id]
    ] : slice(
    data.aws_availability_zones.available.names,
    0,
    min(length(data.aws_availability_zones.available.names), 3)
  )

  # Each AZ's subnets follow from its position. Positions 0-2 are the original
  # layout: a /18 private and a /24 public subnet each. Positions 3-5 take the
  # three /20s left in the fourth /18, whose first /20 holds the public /24s.
  # Only the selected branch is evaluated, so i >= 4 never reaches the /18 form.
  private_subnets = [
    for i in range(length(local.azs)) :
    i < 3 ? cidrsubnet(var.vpc_cidr, 2, i) : cidrsubnet(var.vpc_cidr, 4, 10 + i)
  ]
  public_subnets = [for i in range(length(local.azs)) : cidrsubnet(var.vpc_cidr, 8, 192 + i)]

  # "<az name> <cidr>" at each position, for the subnet guard below.
  private_subnet_layout = [for i, az in local.azs : "${az} ${local.private_subnets[i]}"]

  tags = {
    Cluster = var.cluster_name
    Project = "ciforge"
  }
}

# --- VPC ---

# The VPC module creates subnets, NAT gateways and route tables by list
# position, so removing, reordering or misspelling an AZ (or the
# base.availability_zones key) would plan to destroy live ones. Fail the plan
# instead: every private subnet the cluster already has must keep its AZ and
# CIDR. A new cluster has none yet, so nothing is checked.
data "aws_subnets" "existing_private" {
  filter {
    name   = "tag:Name"
    values = ["${var.cluster_name}-vpc-private-*"]
  }
}

data "aws_subnet" "existing_private" {
  for_each = toset(data.aws_subnets.existing_private.ids)
  id       = each.value

  lifecycle {
    postcondition {
      condition     = contains(local.private_subnet_layout, "${self.availability_zone} ${self.cidr_block}")
      error_message = "Private subnet ${self.id} (${self.availability_zone}, ${self.cidr_block}) is no longer at its position in base.availability_zones. The list is append-only: keep existing entries, in order."
    }
  }
}

module "vpc" {
  source = "./modules/vpc"

  # Apply nothing to the VPC unless the subnet guard passed.
  depends_on = [data.aws_subnet.existing_private]

  name   = "${var.cluster_name}-vpc"
  cidr   = var.vpc_cidr
  azs    = local.azs
  region = var.aws_region

  private_subnets = local.private_subnets
  public_subnets  = local.public_subnets

  enable_nat_gateway      = true
  single_nat_gateway      = var.single_nat_gateway
  nat_secondary_eip_count = var.nat_secondary_eip_count

  tags = merge(local.tags, {
    "kubernetes.io/cluster/${var.cluster_name}" = "shared"
  })

  private_subnet_tags = {}
}

# --- EKS ---

module "eks" {
  source = "./modules/eks"

  aws_region      = var.aws_region
  cluster_name    = var.cluster_name
  cluster_version = var.eks_version
  vpc_id          = module.vpc.vpc_id
  enable_irsa     = true

  # The cluster and base node group stay on the first three AZs. EKS can't
  # move a node group to new subnets (it would be replaced), and a cluster's
  # subnets must stay in the AZs it was created with. Private subnets past the
  # third carry Karpenter nodes only; Karpenter finds them by discovery tag.
  subnet_ids = slice(module.vpc.private_subnet_ids, 0, min(length(module.vpc.private_subnet_ids), 3))

  cluster_endpoint_private_access = true
  cluster_endpoint_public_access  = true

  base_node_count                      = var.base_node_count
  base_node_instance_type              = var.base_node_instance_type
  base_node_max_unavailable_percentage = var.base_node_max_unavailable_percentage
  base_node_ami_version                = var.base_node_ami_version
  base_node_subnet_count               = var.base_node_subnet_count

  cluster_log_types = var.cluster_log_types != "" ? split(",", var.cluster_log_types) : []

  coredns_replicas = var.coredns_replicas

  authentication_mode      = var.authentication_mode
  cluster_admin_role_names = var.cluster_admin_role_names

  tags = local.tags
}

# --- Harbor S3 + IAM ---

module "harbor" {
  source = "./modules/harbor"

  cluster_name      = var.cluster_name
  aws_region        = var.aws_region
  oidc_provider_arn = module.eks.oidc_provider_arn
  oidc_provider     = module.eks.oidc_provider

  tags = local.tags
}
