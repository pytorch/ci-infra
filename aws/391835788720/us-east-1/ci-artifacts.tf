resource "aws_s3_bucket" "pytorch_ci_artifacts" {
  bucket = "pytorch-ci-artifacts"
}

resource "aws_s3_bucket_public_access_block" "pytorch_ci_artifacts_access_block" {
  bucket = aws_s3_bucket.pytorch_ci_artifacts.id

  block_public_acls       = false
  block_public_policy     = false
  ignore_public_acls      = false
  restrict_public_buckets = false
}

resource "aws_s3_bucket_policy" "allow_public_read" {
  bucket = aws_s3_bucket.pytorch_ci_artifacts.id
  policy = data.aws_iam_policy_document.allow_public_read.json
}

data "aws_iam_policy_document" "allow_public_read" {
  statement {
    principals {
      type        = "*"
      identifiers = ["*"]
    }

    actions = [
      "s3:GetObject",
    ]

    resources = [
      "${aws_s3_bucket.pytorch_ci_artifacts.arn}/*",
    ]
  }
}

resource "aws_s3_bucket_website_configuration" "pytorch_ci_artifacts_website" {
  bucket = aws_s3_bucket.pytorch_ci_artifacts.id

  index_document {
    suffix = "index.html"
  }

  error_document {
    key = "error.html"
  }
}

resource "aws_ecr_repository" "pytorch-ci-repo" {
  name = "pytorch"
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = false
  }
}

resource "aws_iam_policy" "pytorch_ci_artifacts_access" {
  name = "pytorch-ci-artifacts-access"
  policy = <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Action": [
        "s3:ListBucket",
        "s3:GetObject",
        "s3:PutObject"
      ],
      "Effect": "Allow",
      "Resource": ["arn:aws:s3:::pytorch-ci-artifacts/*"]
    },
    {
      "Action": [
        "ecr:BatchCheckLayerAvailability",
        "ecr:GetDownloadUrlForLayer",
        "ecr:GetRepositoryPolicy",
        "ecr:DescribeRepositories",
        "ecr:ListImages",
        "ecr:DescribeImages",
        "ecr:BatchGetImage"
      ],
      "Effect": "Allow",
      "Resource": "arn:aws:ecr:us-east-1:***:repository/${aws_ecr_repository.pytorch-ci-repo.name}/*"
    }
  ]
}
EOF
}

resource "aws_iam_role" "gha_pytorch_ci_artifacts_role" {
  name = "gha-pytorch-ci-artifacts-role"

  max_session_duration = 18000
  description = "Used by PyTorch CI to upload artifacts to S3 bucket pytorch-ci-artifacts"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Federated = "arn:aws:iam::${local.aws_account_id}:oidc-provider/token.actions.githubusercontent.com"
        }
        Action = "sts:AssumeRoleWithWebIdentity"
        Condition = {
          StringEquals = {
            "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
          }
          StringLike = {
            "token.actions.githubusercontent.com:sub" = "repo:pytorch/*",
          }
        }
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "pytorch_ci_artifacts_attach" {
  role       = aws_iam_role.gha_pytorch_ci_artifacts_role.name
  policy_arn = aws_iam_policy.pytorch_ci_artifacts_access.arn
}
