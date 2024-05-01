resource "aws_grafana_workspace" "monitoring_production" {
  account_access_type      = "CURRENT_ACCOUNT"
  authentication_providers = ["AWS_SSO"]
  permission_type          = "SERVICE_MANAGED"
  data_sources             = ["PROMETHEUS", "CLOUDWATCH"]
  role_arn                 = aws_iam_role.grafana_assume_production.arn
  name                     = "arc_prod"

  tags = {
    Environment = var.prod_environment
  }
}

resource "aws_iam_role" "grafana_assume_production" {
  name = "grafana_assume_production"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Sid    = ""
        Principal = {
          Service = "grafana.amazonaws.com"
        }
      },
    ]
  })

  tags = {
    Environment = var.prod_environment
  }
}

resource "aws_grafana_workspace" "monitoring_canary" {
  account_access_type      = "CURRENT_ACCOUNT"
  authentication_providers = ["AWS_SSO"]
  permission_type          = "SERVICE_MANAGED"
  data_sources             = ["PROMETHEUS", "CLOUDWATCH"]
  role_arn                 = aws_iam_role.grafana_assume_canary.arn
  name                     = "arc_canary"

  tags = {
    Environment = var.canary_environment
  }
}

resource "aws_iam_role" "grafana_assume_canary" {
  name = "grafana_assume_canary"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Sid    = ""
        Principal = {
          Service = "grafana.amazonaws.com"
        }
      },
    ]
  })

  tags = {
    Environment = var.canary_environment
  }
}
