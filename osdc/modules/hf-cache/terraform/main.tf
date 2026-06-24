# HF Cache module: per-cluster IAM (IRSA) for the shared HuggingFace model cache.
#
# The model cache data lives in a single shared S3 bucket (managed out-of-band by
# terraform/hf-cache-bucket/). This per-cluster root only creates the two IRSA
# roles that the in-cluster service accounts assume:
#
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
  # Per-region bucket (matches terraform/hf-cache-bucket/). Clusters in a region
  # share one bucket but each uses its own <cluster_id>/ prefix, so per-cluster
  # refresh writers never contend over the same keys.
  bucket     = "pytorch-hf-model-cache-${var.aws_region}"
  bucket_arn = "arn:aws:s3:::${local.bucket}"
  # ListBucket stays bucket-wide for simplicity; object access is scoped to the
  # cluster prefix.
  objects_arn = "arn:aws:s3:::${local.bucket}/${var.cluster_id}/*"

  tags = {
    Cluster = var.cluster_name
    Project = "ciforge"
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
