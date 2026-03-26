# PyPI Cache module: AWS infrastructure for EFS-backed pip cache.
#
# Creates EFS filesystem, mount targets, security group, EFS CSI driver addon,
# and IAM role (IRSA) for the CSI controller.
#
# Reads base infrastructure outputs via terraform_remote_state.

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
  oidc_provider_arn  = data.terraform_remote_state.base.outputs.oidc_provider_arn
  oidc_provider      = data.terraform_remote_state.base.outputs.oidc_provider
  vpc_id             = data.terraform_remote_state.base.outputs.vpc_id
  private_subnet_ids = data.terraform_remote_state.base.outputs.private_subnet_ids
  cluster_sg_id      = data.terraform_remote_state.base.outputs.cluster_security_group_id

  tags = {
    Cluster = var.cluster_name
    Project = "ciforge"
  }
}

# --- EFS Filesystem ---

resource "aws_efs_file_system" "pypi_cache" {
  encrypted        = true
  performance_mode = "generalPurpose"
  throughput_mode  = "elastic"

  tags = merge(local.tags, {
    Name                     = "${var.cluster_name}-pypi-cache"
    "karpenter.sh/discovery" = var.cluster_name
  })
}

# --- EFS Security Group ---

resource "aws_security_group" "efs" {
  name_prefix = "${var.cluster_name}-pypi-cache-efs-"
  description = "Allow NFS access from EKS cluster to pypi-cache EFS"
  vpc_id      = local.vpc_id

  ingress {
    description     = "NFS from EKS cluster"
    from_port       = 2049
    to_port         = 2049
    protocol        = "tcp"
    security_groups = [local.cluster_sg_id]
  }

  tags = merge(local.tags, {
    Name = "${var.cluster_name}-pypi-cache-efs"
  })
}

# --- EFS Mount Targets (one per private subnet) ---

resource "aws_efs_mount_target" "pypi_cache" {
  for_each = toset(local.private_subnet_ids)

  file_system_id  = aws_efs_file_system.pypi_cache.id
  subnet_id       = each.value
  security_groups = [aws_security_group.efs.id]
}

# --- EFS CSI Driver IAM Role (IRSA) ---

resource "aws_iam_role" "efs_csi_driver" {
  name = "${var.cluster_name}-efs-csi-driver-role"

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
          "${local.oidc_provider}:sub" = "system:serviceaccount:kube-system:efs-csi-controller-sa"
          "${local.oidc_provider}:aud" = "sts.amazonaws.com"
        }
      }
    }]
  })

  tags = local.tags
}

resource "aws_iam_role_policy_attachment" "efs_csi_driver" {
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonEFSCSIDriverPolicy"
  role       = aws_iam_role.efs_csi_driver.name
}

# --- EFS CSI Driver EKS Addon ---

resource "aws_eks_addon" "efs_csi_driver" {
  cluster_name = var.cluster_name
  addon_name   = "aws-efs-csi-driver"
  # Omit addon_version to use AWS-recommended default version
  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "PRESERVE"

  service_account_role_arn = aws_iam_role.efs_csi_driver.arn

  tags = local.tags

  depends_on = [
    aws_iam_role_policy_attachment.efs_csi_driver,
  ]
}
