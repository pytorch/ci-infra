module "gha-eks-c7g" {
  source = "../../../modules/gha-eks"
  count  = length(var.aws_vpc_suffixes)

  environment = var.prod_environment
  aws_region  = var.aws_region
  aws_account_id = local.aws_account_id
  vpc_id      = module.runners_vpc[count.index].vpc_id
  subnet_ids  = module.runners_vpc[count.index].public_subnets
  aws_vpc_suffix = element(var.aws_vpc_suffixes, count.index)
  eks_cidr_blocks = local.external_k8s_cidr_ipv4
  instance_type = "c7g.4xl"
  ami_type = "AL2_ARM_64"
  cluster_name = "ghci-runners-c7g-4xl"
  capacity_type = "ON_DEMAND"
}

module "gha-eks-g4dn" {
  source = "../../../modules/gha-eks"
  count  = length(var.aws_vpc_suffixes)

  environment = var.prod_environment
  aws_region  = var.aws_region
  aws_account_id = local.aws_account_id
  vpc_id      = module.runners_vpc[count.index].vpc_id
  subnet_ids  = module.runners_vpc[count.index].public_subnets
  aws_vpc_suffix = element(var.aws_vpc_suffixes, count.index)
  eks_cidr_blocks = local.external_k8s_cidr_ipv4
  instance_type = "g4dn.4xl"
  ami_type = "AL2_x86_64_GPU"
  cluster_name = "ghci-runners-g4dn-4xl"
  capacity_type = "ON_DEMAND"
}
