# Standalone terraform root for the shared PyPI wheel cache S3 bucket.
#
# This bucket is shared across all clusters — it lives outside the per-cluster
# terraform state to avoid multiple states managing the same resource.
#
# One-time setup: tofu init && tofu apply

terraform {
  required_version = ">= 1.7"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = "us-east-2"
}

resource "aws_s3_bucket" "wheel_cache" {
  bucket = "pytorch-pypi-wheel-cache"
}

resource "aws_s3_bucket_public_access_block" "wheel_cache" {
  bucket = aws_s3_bucket.wheel_cache.id

  block_public_acls       = false
  block_public_policy     = false
  ignore_public_acls      = false
  restrict_public_buckets = false
}

resource "aws_s3_bucket_policy" "wheel_cache" {
  bucket = aws_s3_bucket.wheel_cache.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "PublicReadWantsAndCache"
      Effect    = "Allow"
      Principal = "*"
      Action    = "s3:GetObject"
      Resource = [
        "${aws_s3_bucket.wheel_cache.arn}/wants/*",
        "${aws_s3_bucket.wheel_cache.arn}/prebuilt-cache.txt",
        "${aws_s3_bucket.wheel_cache.arn}/needbuild.txt",
      ]
    }]
  })

  depends_on = [aws_s3_bucket_public_access_block.wheel_cache]
}

resource "aws_s3_bucket_lifecycle_configuration" "wheel_cache" {
  bucket = aws_s3_bucket.wheel_cache.id

  rule {
    id     = "expire-wants"
    status = "Enabled"

    filter {
      prefix = "wants/"
    }

    expiration {
      days = 7
    }
  }
}
