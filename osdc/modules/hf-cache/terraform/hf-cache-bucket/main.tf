# Standalone terraform root for the shared HuggingFace model-cache S3 bucket.
#
# This bucket is shared across all clusters — it lives outside the per-cluster
# terraform state to avoid multiple states managing the same resource. It holds
# the model cache as plain HuggingFace cache-layout files (symlink-free), which
# are the portable source of truth: any object store / cloud can host the same
# layout and be `rclone sync`'d here or away.
#
# Unlike the pypi wheel cache, this bucket is PRIVATE — access is granted only
# through the per-cluster IRSA roles in ../main.tf. No public read.
#
# One-time setup:
#   tofu init -backend-config=... && tofu apply

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

resource "aws_s3_bucket" "hf_cache" {
  bucket = "pytorch-hf-model-cache"
}

resource "aws_s3_bucket_public_access_block" "hf_cache" {
  bucket = aws_s3_bucket.hf_cache.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "hf_cache" {
  bucket = aws_s3_bucket.hf_cache.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# Reap incomplete multipart uploads left behind by interrupted refresh runs.
resource "aws_s3_bucket_lifecycle_configuration" "hf_cache" {
  bucket = aws_s3_bucket.hf_cache.id

  rule {
    id     = "abort-incomplete-multipart"
    status = "Enabled"

    filter {}

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}
