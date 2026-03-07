# Base infrastructure: VPC + EKS + Harbor S3/IAM
#
# This is cluster-agnostic. All cluster-specific values come from variables
# (passed via -var flags from justfile, which reads clusters.yaml).

data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  azs = slice(
    data.aws_availability_zones.available.names,
    0,
    min(length(data.aws_availability_zones.available.names), 3)
  )

  tags = {
    Cluster = var.cluster_name
    Project = "ciforge"
  }
}

# --- VPC ---

module "vpc" {
  source = "./modules/vpc"

  name = "${var.cluster_name}-vpc"
  cidr = var.vpc_cidr
  azs  = local.azs

  # Dynamic subnet sizing based on AZ count
  private_subnets = length(local.azs) == 2 ? [
    cidrsubnet(var.vpc_cidr, 2, 0),
    cidrsubnet(var.vpc_cidr, 2, 1),
    ] : [
    cidrsubnet(var.vpc_cidr, 2, 0),
    cidrsubnet(var.vpc_cidr, 2, 1),
    cidrsubnet(var.vpc_cidr, 2, 2),
  ]

  public_subnets = length(local.azs) == 2 ? [
    cidrsubnet(var.vpc_cidr, 8, 192),
    cidrsubnet(var.vpc_cidr, 8, 193),
    ] : [
    cidrsubnet(var.vpc_cidr, 8, 192),
    cidrsubnet(var.vpc_cidr, 8, 193),
    cidrsubnet(var.vpc_cidr, 8, 194),
  ]

  enable_nat_gateway = true
  single_nat_gateway = var.single_nat_gateway

  tags = merge(local.tags, {
    "kubernetes.io/cluster/${var.cluster_name}" = "shared"
  })

  private_subnet_tags = {
    "karpenter.sh/discovery" = var.cluster_name
  }
}

# --- EKS ---

module "eks" {
  source = "./modules/eks"

  aws_region      = var.aws_region
  cluster_name    = var.cluster_name
  cluster_version = var.eks_version
  vpc_id          = module.vpc.vpc_id
  subnet_ids      = module.vpc.private_subnet_ids
  enable_irsa     = true

  cluster_endpoint_private_access = true
  cluster_endpoint_public_access  = true

  base_node_count                      = var.base_node_count
  base_node_instance_type              = var.base_node_instance_type
  base_node_max_unavailable_percentage = var.base_node_max_unavailable_percentage

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
