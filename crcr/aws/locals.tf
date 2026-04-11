locals {
  secret_store_arn = aws_secretsmanager_secret.main.arn

  availability_zones = [
    for suffix in var.availability_zone_suffixes :
    "${local.aws_region}${suffix}"
  ]

  tags = {
    Project     = "cross-repo-ci-relay"
    Environment = var.environment
  }
}
