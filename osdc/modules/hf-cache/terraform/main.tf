# Per-cluster private S3 bucket (pytorch-hf-model-cache-<cluster_id>, in the
# cluster's region) + a read-only IRSA role for the rclone mount. Created in the
# cluster's own state, so `just deploy-module <cluster> hf-cache` provisions it.

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
  region = var.aws_region
}

data "terraform_remote_state" "base" {
  backend = "s3"

  config = {
    bucket         = var.state_bucket
    key            = "${var.cluster_id}/base/terraform.tfstate"
    region         = "us-west-2"
    dynamodb_table = "ciforge-terraform-locks"
  }
}

locals {
  oidc_provider_arn = data.terraform_remote_state.base.outputs.oidc_provider_arn
  oidc_provider     = data.terraform_remote_state.base.outputs.oidc_provider

  namespace = "hf-cache"
  # One private bucket per cluster; name from cluster_id (globally unique).
  bucket      = "pytorch-hf-model-cache-${var.cluster_id}"
  bucket_arn  = "arn:aws:s3:::${local.bucket}"
  objects_arn = "arn:aws:s3:::${local.bucket}/*"

  tags = {
    Cluster = var.cluster_name
    Project = "ciforge"
  }
}

# --- Per-cluster model-cache bucket ---
# force_destroy=true is safe: the cache is reproducible (refreshed daily), so
# teardown can drop a non-empty bucket instead of erroring.

resource "aws_s3_bucket" "hf_cache" {
  bucket = local.bucket

  force_destroy = true

  tags = local.tags
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

# Reap incomplete multipart uploads.
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

# --- IRSA role for the rclone mount (read-only) ---
# Runners only read the cache. Writes go through a GitHub-OIDC role in
# pytorch-gha-infra (gha_workflow_hf-cache-write), assumed by ci-refresh-hf-cache
# runs — so untrusted job pods can't write.

resource "aws_iam_role" "hf_cache" {
  name = "${var.cluster_name}-hf-cache-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action = "sts:AssumeRoleWithWebIdentity"
      Effect = "Allow"
      Principal = {
        Federated = local.oidc_provider_arn
      }
      Condition = {
        StringEquals = {
          "${local.oidc_provider}:aud" = "sts.amazonaws.com"
          "${local.oidc_provider}:sub" = "system:serviceaccount:${local.namespace}:hf-cache-mount"
        }
      }
    }]
  })

  tags = local.tags
}

resource "aws_iam_policy" "hf_cache" {
  name = "${var.cluster_name}-hf-cache-s3"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "s3:GetObject",
        "s3:ListBucket",
      ]
      Resource = [
        local.bucket_arn,
        local.objects_arn,
      ]
    }]
  })

  tags = local.tags
}

resource "aws_iam_role_policy_attachment" "hf_cache" {
  policy_arn = aws_iam_policy.hf_cache.arn
  role       = aws_iam_role.hf_cache.name
}
