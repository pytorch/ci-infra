locals {
  cluster_name = "${var.environment}-runners-eks-${var.aws_vpc_suffix}"
  kms_users = concat([
      "arn:aws:iam::${var.aws_account_id}:root",
    ],
    var.additional_kms_users
  )
  eks_users = concat([
      ["arn:aws:iam::${var.aws_account_id}:root", "admin"],
      ["arn:aws:iam::${var.aws_account_id}:user/ossci", "admin"],
    ],
    [
      for user in var.additional_eks_users:
      [user, "admin"]
    ]
  )
}
