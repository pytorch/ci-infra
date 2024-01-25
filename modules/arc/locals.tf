locals {
  cluster_name = "${var.environment}-runners-eks-${var.aws_vpc_suffix}"
  kms_users = [
    "arn:aws:iam::${var.aws_account_id}:root",
    "arn:aws:iam::308535385114:user/ossci",
  ]
  eks_users = [
    ["arn:aws:iam::${var.aws_account_id}:root", "admin"],
    ["arn:aws:iam::308535385114:user/ossci", "admin"],
  ]
}
