locals {
  secret_store_arn = "arn:aws:secretsmanager:${local.aws_region}:${local.aws_account_id}:secret:${var.secret_name}"

  availability_zones = [
    for suffix in var.availability_zone_suffixes :
    "${local.aws_region}${suffix}"
  ]

  tags = {
    Project     = "cross-repo-ci-relay"
    Environment = var.environment
  }
}
