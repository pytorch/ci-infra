terraform {
  required_version = ">= 1.7"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

# S3 Bucket for Harbor registry storage (cached layers)
resource "aws_s3_bucket" "harbor_registry" {
  bucket = "${var.cluster_name}-harbor-registry"

  tags = var.tags
}

# Lifecycle rule: expire cached layers after 30 days
resource "aws_s3_bucket_lifecycle_configuration" "harbor_registry" {
  bucket = aws_s3_bucket.harbor_registry.id

  rule {
    id     = "expire-cached-layers"
    status = "Enabled"

    filter {}

    expiration {
      days = 30
    }
  }
}

# Block public access
resource "aws_s3_bucket_public_access_block" "harbor_registry" {
  bucket = aws_s3_bucket.harbor_registry.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Enable server-side encryption
resource "aws_s3_bucket_server_side_encryption_configuration" "harbor_registry" {
  bucket = aws_s3_bucket.harbor_registry.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# Harbor Registry IAM Role (IRSA)
resource "aws_iam_role" "harbor_registry" {
  name = "${var.cluster_name}-harbor-registry"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action = "sts:AssumeRoleWithWebIdentity"
      Effect = "Allow"
      Principal = {
        Federated = var.oidc_provider_arn
      }
      Condition = {
        StringEquals = {
          "${var.oidc_provider}:sub" = "system:serviceaccount:harbor-system:harbor-registry"
          "${var.oidc_provider}:aud" = "sts.amazonaws.com"
        }
      }
    }]
  })

  tags = var.tags
}

# Harbor Registry S3 Policy
resource "aws_iam_policy" "harbor_registry" {
  name        = "${var.cluster_name}-harbor-registry"
  description = "Harbor registry S3 access for ${var.cluster_name}"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowS3BucketAccess"
        Effect = "Allow"
        Action = [
          "s3:ListBucket",
          "s3:GetBucketLocation",
          "s3:ListBucketMultipartUploads"
        ]
        Resource = aws_s3_bucket.harbor_registry.arn
      },
      {
        Sid    = "AllowS3ObjectAccess"
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:PutObject",
          "s3:DeleteObject",
          "s3:ListMultipartUploadParts",
          "s3:AbortMultipartUpload"
        ]
        Resource = "${aws_s3_bucket.harbor_registry.arn}/*"
      }
    ]
  })

  tags = var.tags
}

resource "aws_iam_role_policy_attachment" "harbor_registry" {
  role       = aws_iam_role.harbor_registry.name
  policy_arn = aws_iam_policy.harbor_registry.arn
}

# IAM User for Harbor S3 access (static credentials)
# The goharbor/distribution S3 driver hardcodes its AWS credential chain
# and does not support IRSA (web identity tokens). A static IAM user is
# the simplest workaround until the upstream driver adds IRSA support.
resource "aws_iam_user" "harbor_s3" {
  name = "${var.cluster_name}-harbor-s3"
  tags = var.tags
}

resource "aws_iam_user_policy_attachment" "harbor_s3" {
  user       = aws_iam_user.harbor_s3.name
  policy_arn = aws_iam_policy.harbor_registry.arn
}

resource "aws_iam_access_key" "harbor_s3" {
  user = aws_iam_user.harbor_s3.name
}
