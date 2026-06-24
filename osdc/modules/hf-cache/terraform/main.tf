# HF Cache module: per-cluster S3 bucket + IAM (IRSA) for the HuggingFace cache.
#
# Each cluster gets its OWN private bucket (pytorch-hf-model-cache-<cluster_id>)
# in the cluster's region, created here in the cluster's own terraform state —
# so `just deploy-module <cluster> hf-cache` provisions everything; there is no
# separate, manual bucket-apply step. Because the bucket is cluster-scoped, no
# prefix partitioning is needed and reads stay in-region.
#
# Plus the two IRSA roles the in-cluster service accounts assume:
#   * hf-cache-mount    — read-only. The rclone-mount DaemonSet uses it to expose
#                         the bucket as a read-only FUSE mount on each node.
#   * hf-cache-refresh  — read/write. The refresh CronJob uses it to publish newly
#                         downloaded models back to the bucket.
#
# Reads base infrastructure outputs (OIDC provider) via terraform_remote_state.

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
  # One private bucket per cluster (globally-unique name from cluster_id),
  # created below in this cluster's own state. No prefix partitioning needed.
  bucket      = "pytorch-hf-model-cache-${var.cluster_id}"
  bucket_arn  = "arn:aws:s3:::${local.bucket}"
  objects_arn = "arn:aws:s3:::${local.bucket}/*"

  tags = {
    Cluster = var.cluster_name
    Project = "ciforge"
  }
}

# --- Model-cache S3 bucket (one per cluster, in the cluster's region) ---
#
# Holds the cache as plain symlink-free HuggingFace cache-layout files — the
# portable source of truth (any object store / cloud can host the same layout
# and be `rclone sync`'d here or away). Private; access only via the IRSA roles
# below. force_destroy stays false so `remove-module` won't silently wipe a
# populated cache (tofu destroy fails on a non-empty bucket; empty it manually
# if you really mean to delete it).

resource "aws_s3_bucket" "hf_cache" {
  bucket = local.bucket

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

# --- Mount (reader) IAM Role (IRSA) ---

resource "aws_iam_role" "mount" {
  name = "${var.cluster_name}-hf-cache-mount-role"

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

resource "aws_iam_policy" "mount" {
  name = "${var.cluster_name}-hf-cache-mount-s3"

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

resource "aws_iam_role_policy_attachment" "mount" {
  policy_arn = aws_iam_policy.mount.arn
  role       = aws_iam_role.mount.name
}

# --- Refresh (writer) IAM Role (IRSA) ---

resource "aws_iam_role" "refresh" {
  name = "${var.cluster_name}-hf-cache-refresh-role"

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
          "${local.oidc_provider}:sub" = "system:serviceaccount:${local.namespace}:hf-cache-refresh"
        }
      }
    }]
  })

  tags = local.tags
}

resource "aws_iam_policy" "refresh" {
  name = "${var.cluster_name}-hf-cache-refresh-s3"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "s3:GetObject",
        "s3:PutObject",
        "s3:DeleteObject",
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

resource "aws_iam_role_policy_attachment" "refresh" {
  policy_arn = aws_iam_policy.refresh.arn
  role       = aws_iam_role.refresh.name
}
