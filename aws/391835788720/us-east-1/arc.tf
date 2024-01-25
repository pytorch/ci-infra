module "arc_canary" {
    source = "../../../modules/arc"
    for_each = {
        for env in var.aws_vpc_suffixes:
        env => module.runners_canary_vpc[env]
    }

    environment = var.canary_environment
    vpc_id = each.value.vpc_id
    subnet_ids = each.value.public_subnets
    aws_vpc_suffix = each.key
    eks_cidr_blocks = local.external_k8s_cidr_ipv4
}

module "arc_vanguard" {
    source = "../../../modules/arc"
    for_each = {
        for env in var.aws_vpc_suffixes:
        env => module.runners_canary_vpc[env]
    }

    environment = var.vanguard_environment
    vpc_id = each.value.vpc_id
    subnet_ids = each.value.public_subnets
    aws_vpc_suffix = each.key
    eks_cidr_blocks = local.external_k8s_cidr_ipv4
}

module "arc_prod" {
    source = "../../../modules/arc"
    for_each = {
        for env in var.aws_vpc_suffixes:
        env => module.runners_canary_vpc[env]
    }

    environment = var.prod_environment
    vpc_id = each.value.vpc_id
    subnet_ids = each.value.public_subnets
    aws_vpc_suffix = each.key
    eks_cidr_blocks = local.external_k8s_cidr_ipv4
}
