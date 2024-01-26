module "arc_canary" {
    source = "../../../modules/arc"
    for_each = {
        for env in var.aws_canary_vpc_suffixes:
        env => module.runners_canary_vpc[env]
    }

    additional_eks_users = [ aws_iam_user.ossci.arn ]
    additional_kms_users = [ aws_iam_user.ossci.arn ]
    aws_vpc_suffix = each.key
    eks_cidr_blocks = local.external_k8s_cidr_ipv4
    environment = var.canary_environment
    subnet_ids = each.value.public_subnets
    vpc_id = each.value.vpc_id
}

module "arc_vanguard" {
    source = "../../../modules/arc"
    for_each = {
        for env in var.aws_vpc_suffixes:
        env => module.runners_vpc[env]
    }

    additional_eks_users = [ aws_iam_user.ossci.arn ]
    additional_kms_users = [ aws_iam_user.ossci.arn ]
    aws_vpc_suffix = each.key
    eks_cidr_blocks = local.external_k8s_cidr_ipv4
    environment = var.vanguard_environment
    subnet_ids = each.value.public_subnets
    vpc_id = each.value.vpc_id
}

module "arc_prod" {
    source = "../../../modules/arc"
    for_each = {
        for env in var.aws_vpc_suffixes:
        env => module.runners_vpc[env]
    }

    additional_eks_users = [ aws_iam_user.ossci.arn ]
    additional_kms_users = [ aws_iam_user.ossci.arn ]
    aws_vpc_suffix = each.key
    eks_cidr_blocks = local.external_k8s_cidr_ipv4
    environment = var.prod_environment
    subnet_ids = each.value.public_subnets
    vpc_id = each.value.vpc_id
}
