resource "aws_iam_user" "ossci" {
  name = "ossci"

  tags = {
    Project = "runners-eks"
  }
}

resource "aws_iam_role" "ossci_gha_terraform" {
  name = "ossci_gha_terraform"

  max_session_duration = 18000
  description = "used by pytorch-labs/pytorch-gha-infra workflows to deploy terraform configs"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Federated = "arn:aws:iam::${local.aws_region}:oidc-provider/token.actions.githubusercontent.com"
        }
        Action = "sts:AssumeRoleWithWebIdentity"
        Condition = {
          StringEquals = {
            "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
          }
          StringLike = {
            "token.actions.githubusercontent.com:sub" = "repo:pytorch/ci-infra:*"
          }
        }
      }
    ]
  })

  tags = {
    project = var.prod_environment
    environment = "${var.prod_environment}-workflows"
  }
}

resource "aws_iam_role_policy_attachment" "ossci_gha_terraform_admin" {
  role       = aws_iam_role.ossci_gha_terraform.name
  policy_arn = "arn:aws:iam::aws:policy/AdministratorAccess"
}
