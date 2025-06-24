module "arc_canary_monitoring" {
    source = "../../../modules/arc-monitoring"

    additional_eks_users = [ aws_iam_user.ossci.arn ]
    additional_kms_users = [ aws_iam_user.ossci.arn ]
    aws_account_id = local.aws_account_id
    aws_vpc_suffix = "I"
    eks_cidr_blocks = local.external_k8s_cidr_ipv4
    environment = var.arc_canary_environment
    subnet_ids = module.arc_runners_canary_vpc["I"].private_subnets
    vpc_id = module.arc_runners_canary_vpc["I"].vpc_id
}

module "arc_prod_monitoring" {
    source = "../../../modules/arc-monitoring"

    additional_eks_users = [ aws_iam_user.ossci.arn ]
    additional_kms_users = [ aws_iam_user.ossci.arn ]
    aws_account_id = local.aws_account_id
    aws_vpc_suffix = "I"
    eks_cidr_blocks = local.external_k8s_cidr_ipv4
    environment = var.arc_prod_environment
    subnet_ids = module.arc_runners_vpc["I"].private_subnets
    vpc_id = module.arc_runners_vpc["I"].vpc_id
}
