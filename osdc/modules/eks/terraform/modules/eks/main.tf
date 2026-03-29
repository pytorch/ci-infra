terraform {
  required_version = ">= 1.7"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    null = {
      source  = "hashicorp/null"
      version = "~> 3.0"
    }
  }
}

# Current AWS account ID — used to construct role ARNs for access entries
data "aws_caller_identity" "current" {}

locals {
  cluster_admin_roles = var.cluster_admin_role_names != "" ? split(",", var.cluster_admin_role_names) : []
}

# Get EKS-optimized AMI for Amazon Linux 2023 (pin version via base_node_ami_version)
data "aws_ami" "eks_optimized_al2023" {
  most_recent = true
  owners      = ["amazon"]

  filter {
    name   = "name"
    values = ["amazon-eks-node-al2023-x86_64-standard-${var.cluster_version}-${var.base_node_ami_version}"]
  }

  filter {
    name   = "architecture"
    values = ["x86_64"]
  }

  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
}

# KMS key for EKS secrets envelope encryption at rest
resource "aws_kms_key" "eks_secrets" {
  count = var.enable_secrets_encryption ? 1 : 0

  description             = "KMS key for EKS secrets encryption - ${var.cluster_name}"
  deletion_window_in_days = 30
  enable_key_rotation     = true

  tags = merge(
    var.tags,
    {
      Name = "${var.cluster_name}-eks-secrets"
    }
  )
}

resource "aws_kms_alias" "eks_secrets" {
  count = var.enable_secrets_encryption ? 1 : 0

  name          = "alias/${var.cluster_name}-eks-secrets"
  target_key_id = aws_kms_key.eks_secrets[0].key_id
}

# EKS Cluster
resource "aws_eks_cluster" "this" {
  name     = var.cluster_name
  version  = var.cluster_version
  role_arn = aws_iam_role.cluster.arn

  access_config {
    authentication_mode                         = var.authentication_mode
    bootstrap_cluster_creator_admin_permissions = var.bootstrap_cluster_creator_admin_permissions
  }

  vpc_config {
    subnet_ids              = var.subnet_ids
    endpoint_private_access = var.cluster_endpoint_private_access
    endpoint_public_access  = var.cluster_endpoint_public_access
    public_access_cidrs     = var.cluster_endpoint_public_access_cidrs
  }

  # Encrypt Kubernetes secrets at rest using KMS envelope encryption
  dynamic "encryption_config" {
    for_each = var.enable_secrets_encryption ? [1] : []
    content {
      provider {
        key_arn = aws_kms_key.eks_secrets[0].arn
      }
      resources = ["secrets"]
    }
  }

  enabled_cluster_log_types = ["api", "audit", "authenticator", "controllerManager", "scheduler"]

  tags = merge(
    var.tags,
    {
      Name = var.cluster_name
    }
  )

  lifecycle {
    ignore_changes = [
      # Karpenter's deploy script adds this tag out-of-band via `aws eks tag-resource`.
      # Without ignoring it, every tofu plan shows drift.
      tags["karpenter.sh/discovery"],
    ]
  }

  depends_on = [
    aws_iam_role_policy_attachment.cluster_policy,
    aws_iam_role_policy_attachment.vpc_resource_controller,
  ]
}

# EKS Cluster IAM Role
resource "aws_iam_role" "cluster" {
  name = "${var.cluster_name}-cluster-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "eks.amazonaws.com"
        }
      }
    ]
  })

  tags = var.tags
}

resource "aws_iam_role_policy_attachment" "cluster_policy" {
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSClusterPolicy"
  role       = aws_iam_role.cluster.name
}

resource "aws_iam_role_policy_attachment" "vpc_resource_controller" {
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSVPCResourceController"
  role       = aws_iam_role.cluster.name
}

# OIDC Provider for IRSA
data "tls_certificate" "cluster" {
  count = var.enable_irsa ? 1 : 0
  url   = aws_eks_cluster.this.identity[0].oidc[0].issuer
}

resource "aws_iam_openid_connect_provider" "cluster" {
  count = var.enable_irsa ? 1 : 0

  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = [data.tls_certificate.cluster[0].certificates[0].sha1_fingerprint]
  url             = aws_eks_cluster.this.identity[0].oidc[0].issuer

  tags = var.tags
}

# EKS Addons
resource "aws_eks_addon" "vpc_cni" {
  cluster_name                = aws_eks_cluster.this.name
  addon_name                  = "vpc-cni"
  addon_version               = "v1.21.1-eksbuild.3"
  resolve_conflicts_on_update = "PRESERVE"

  tags = var.tags
}

resource "aws_eks_addon" "coredns" {
  cluster_name                = aws_eks_cluster.this.name
  addon_name                  = "coredns"
  addon_version               = "v1.13.2-eksbuild.1"
  resolve_conflicts_on_update = "PRESERVE"

  # CoreDNS must tolerate base node taints to run on infrastructure nodes
  configuration_values = jsonencode({
    tolerations = [
      {
        key      = "CriticalAddonsOnly"
        operator = "Equal"
        value    = "true"
        effect   = "NoSchedule"
      }
    ]
  })

  tags = var.tags

  depends_on = [aws_eks_node_group.base]
}

resource "aws_eks_addon" "kube_proxy" {
  cluster_name                = aws_eks_cluster.this.name
  addon_name                  = "kube-proxy"
  addon_version               = "v1.35.0-eksbuild.2"
  resolve_conflicts_on_update = "PRESERVE"

  tags = var.tags
}

# EBS CSI Driver IAM Role (IRSA)
data "aws_iam_policy_document" "ebs_csi_assume_role" {
  count = var.enable_irsa ? 1 : 0

  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    effect  = "Allow"

    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.cluster[0].arn]
    }

    condition {
      test     = "StringEquals"
      variable = "${replace(aws_eks_cluster.this.identity[0].oidc[0].issuer, "https://", "")}:sub"
      values   = ["system:serviceaccount:kube-system:ebs-csi-controller-sa"]
    }

    condition {
      test     = "StringEquals"
      variable = "${replace(aws_eks_cluster.this.identity[0].oidc[0].issuer, "https://", "")}:aud"
      values   = ["sts.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "ebs_csi_driver" {
  count = var.enable_irsa ? 1 : 0

  name               = "${var.cluster_name}-ebs-csi-driver-role"
  assume_role_policy = data.aws_iam_policy_document.ebs_csi_assume_role[0].json

  tags = var.tags
}

resource "aws_iam_role_policy_attachment" "ebs_csi_driver" {
  count = var.enable_irsa ? 1 : 0

  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy"
  role       = aws_iam_role.ebs_csi_driver[0].name
}

resource "aws_eks_addon" "ebs_csi_driver" {
  cluster_name = aws_eks_cluster.this.name
  addon_name   = "aws-ebs-csi-driver"
  # Omit addon_version to use AWS-recommended default version
  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "PRESERVE"

  service_account_role_arn = var.enable_irsa ? aws_iam_role.ebs_csi_driver[0].arn : null

  tags = var.tags

  depends_on = [
    aws_eks_node_group.base,
    aws_iam_role_policy_attachment.ebs_csi_driver,
  ]
}

# Base Infrastructure Node Group (Fixed Size)
# These nodes run critical cluster components only (ARC, Karpenter, CoreDNS, etc.)
# Tainted to prevent runner workloads from scheduling here
resource "aws_eks_node_group" "base" {
  cluster_name    = aws_eks_cluster.this.name
  node_group_name = "${var.cluster_name}-base-nodes"
  node_role_arn   = aws_iam_role.node.arn
  subnet_ids      = var.subnet_ids

  # Fixed size - no auto-scaling
  scaling_config {
    desired_size = var.base_node_count
    max_size     = var.base_node_count
    min_size     = var.base_node_count
  }

  # AGGRESSIVE UPDATE STRATEGY: Replace all nodes immediately
  # No pod drainage, no waiting - maximum speed for infrastructure changes
  update_config {
    max_unavailable_percentage = var.base_node_max_unavailable_percentage
  }

  # Force immediate update without waiting for nodes to be ready
  force_update_version = true

  instance_types = [var.base_node_instance_type]
  capacity_type  = "ON_DEMAND"

  labels = {
    role                           = "base-infrastructure"
    "node.kubernetes.io/lifecycle" = "on-demand"
  }

  # Taint to prevent runner workloads from landing here
  # Only system components with matching tolerations can schedule
  taint {
    key    = "CriticalAddonsOnly"
    value  = "true"
    effect = "NO_SCHEDULE"
  }

  # Use launch template for bootstrap script
  launch_template {
    id      = aws_launch_template.base.id
    version = aws_launch_template.base.latest_version
  }

  # Force immediate updates - no grace period
  lifecycle {
    ignore_changes = [
      scaling_config[0].desired_size, # Allow manual scaling without Terraform recreation
    ]
    create_before_destroy = false # Destroy old nodes immediately, don't wait
  }

  # Reduced timeouts for faster feedback
  timeouts {
    create = "15m" # Reduced from 30m
    update = "15m" # Reduced from 30m
    delete = "10m" # Reduced from 30m
  }

  tags = merge(
    var.tags,
    {
      Name = "${var.cluster_name}-base-nodes"
      Type = "base-infrastructure"
    }
  )

  depends_on = [
    aws_iam_role_policy_attachment.node_policy,
    aws_iam_role_policy_attachment.cni_policy,
    aws_iam_role_policy_attachment.ecr_policy,
    aws_iam_role_policy_attachment.ssm_policy,
  ]
}

# Launch template for base infrastructure nodes
resource "aws_launch_template" "base" {
  name_prefix = "${var.cluster_name}-base-"
  image_id    = data.aws_ami.eks_optimized_al2023.id
  # instance_type removed - specified in node group instead

  # User data for AL2023 with nodeadm configuration
  user_data = base64encode(templatefile("${path.module}/user-data-base.sh.tpl", {
    cluster_name          = aws_eks_cluster.this.name
    cluster_endpoint      = aws_eks_cluster.this.endpoint
    cluster_ca_data       = aws_eks_cluster.this.certificate_authority[0].data
    service_cidr          = aws_eks_cluster.this.kubernetes_network_config[0].service_ipv4_cidr
    post_bootstrap_script = file("${path.root}/../../../base/scripts/bootstrap/eks-base-bootstrap.sh")
  }))

  block_device_mappings {
    device_name = "/dev/xvda"

    ebs {
      volume_size           = 100
      volume_type           = "gp3"
      iops                  = 3000
      throughput            = 125
      delete_on_termination = true
      encrypted             = true
    }
  }

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  tag_specifications {
    resource_type = "instance"
    tags = merge(
      var.tags,
      {
        Name = "${var.cluster_name}-base-node"
        Type = "base-infrastructure"
      }
    )
  }

  tags = var.tags
}

# EKS Node IAM Role
resource "aws_iam_role" "node" {
  name = "${var.cluster_name}-node-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "ec2.amazonaws.com"
        }
      }
    ]
  })

  tags = var.tags
}

resource "aws_iam_role_policy_attachment" "node_policy" {
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy"
  role       = aws_iam_role.node.name
}

resource "aws_iam_role_policy_attachment" "cni_policy" {
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy"
  role       = aws_iam_role.node.name
}

resource "aws_iam_role_policy_attachment" "ecr_policy" {
  policy_arn = "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly"
  role       = aws_iam_role.node.name
}

resource "aws_iam_role_policy_attachment" "ssm_policy" {
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
  role       = aws_iam_role.node.name
}

# --- EKS Access Entries (requires API or API_AND_CONFIG_MAP authentication mode) ---

resource "aws_eks_access_entry" "cluster_admin" {
  for_each = toset(local.cluster_admin_roles)

  cluster_name  = aws_eks_cluster.this.name
  principal_arn = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/${each.value}"
  type          = "STANDARD"

  tags = var.tags
}

resource "aws_eks_access_policy_association" "cluster_admin" {
  for_each = toset(local.cluster_admin_roles)

  cluster_name  = aws_eks_cluster.this.name
  policy_arn    = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"
  principal_arn = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/${each.value}"

  access_scope {
    type = "cluster"
  }

  depends_on = [aws_eks_access_entry.cluster_admin]
}
