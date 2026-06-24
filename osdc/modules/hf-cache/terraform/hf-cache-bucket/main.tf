# Standalone terraform root for a per-region HuggingFace model-cache S3 bucket.
#
# One bucket per region (pytorch-hf-model-cache-<region>), shared by all clusters
# in that region — it lives outside per-cluster state because multiple clusters
# share it. Each cluster reads/writes only its own <cluster_id>/ prefix, so the
# per-cluster refresh writers never contend. A same-region bucket means runners
# read without cross-region S3 egress/latency.
#
# It holds the model cache as plain HuggingFace cache-layout files (symlink-free),
# the portable source of truth: any object store / cloud can host the same layout
# and be `rclone sync`'d here or away.
#
# Unlike the pypi wheel cache, this bucket is PRIVATE — access is granted only
# through the per-cluster IRSA roles in ../main.tf. No public read.
#
# Apply once per region (local state, isolated per region via a tofu workspace):
#   just hf-cache-bucket <region>

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
  region = var.region
}

resource "aws_s3_bucket" "hf_cache" {
  bucket = "pytorch-hf-model-cache-${var.region}"
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
