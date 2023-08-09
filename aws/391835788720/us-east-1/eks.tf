module "gha-eks" {
  source = "../../../modules/gha-eks"
  count  = length(var.aws_vpc_suffixes)

  environment = var.prod_environment
  aws_region  = var.aws_region
  aws_account_id = local.aws_account_id
  vpc_id      = module.runners_vpc[count.index].vpc_id
  subnet_ids  = module.runners_vpc[count.index].public_subnets
  aws_vpc_suffix = element(var.aws_vpc_suffixes, count.index)
  eks_cidr_blocks = local.external_k8s_cidr_ipv4
}
