
resource "aws_iam_role" "gha_target_determinator_s3_read_write" {
  name = "gha_target_determinator_s3_read_write"

  max_session_duration = 18000
  description = "Allows PyTorch Target Determinator GHA to have read/write access to s3://target-determinator-assets/ bucket"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Federated = "arn:aws:iam::308535385114:oidc-provider/token.actions.githubusercontent.com"
        }
        Action = "sts:AssumeRoleWithWebIdentity"
        Condition = {
          StringEquals = {
            "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
          }
          StringLike = {
            "token.actions.githubusercontent.com:sub" = "repo:pytorch/pytorch:environment:target-determinator-env"
          }
        }
      }
    ]
  })

  inline_policy {
    name = "gha_target_determinator_s3_read_write"
    policy = jsonencode({
      Version = "2012-10-17"
      Statement = [
        {
          Action   = [
            "s3:GetObject*",
            "s3:GetBucket*",
            "s3:PutObject*",
            "s3:ListBucket*",
            "s3:DeleteObject",
          ]
          Effect   = "Allow"
          Resource = [
            "arn:aws:s3:::target-determinator-assets/*",
            "arn:aws:s3:::target-determinator-assets"
          ]
        },
      ]
    })
  }

  tags = {
    project = var.ali_prod_environment
    environment = "pytorch-target-determinator-workflows"
    workflow = "target-determinator-indexer"
  }
}
