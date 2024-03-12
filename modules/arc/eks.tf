module "eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "~> 19.0"

  cluster_name    = local.cluster_name
  cluster_version = "1.29"

  cluster_endpoint_public_access  = true

  cluster_security_group_additional_rules = {
    ingress = {
      description                = "To node 1025-65535"
      type                       = "ingress"
      from_port                  = 0
      to_port                    = 0
      protocol                   = -1
      cidr_blocks                = var.eks_cidr_blocks
      ipv6_cidr_blocks           = []
      source_node_security_group = false
    }
  }

  cluster_addons = {
    coredns = {
      most_recent = true
    }
    kube-proxy = {
      most_recent = true
    }
    vpc-cni = {
      most_recent = true
    }
    aws-ebs-csi-driver = {
      most_recent = true
      allow_volume_expansion = false
    }
  }

  cluster_enabled_log_types = [
    "api",
    "audit",
    "authenticator",
    "controllerManager",
    "scheduler",
  ]

  vpc_id                   = var.vpc_id
  subnet_ids               = var.subnet_ids

  eks_managed_node_group_defaults = {
    instance_types = [var.basic_instance_type]
    ami_type       = "AL2_x86_64"
  }

  eks_managed_node_groups = {
    green = {
      min_size     = 1
      max_size     = 20
      desired_size = 1

      taints = [
        {
          key    = "CriticalAddonsOnly"
          effect = "NO_SCHEDULE"
        }
      ]

      instance_types = [var.basic_instance_type]
      ami_type       = "AL2_x86_64"
      capacity_type  = "SPOT"
      labels = {
        Project     = var.environment
        Environment = local.cluster_name
      }

      update_config = {
        max_unavailable_percentage = 33
      }

      iam_role_additional_policies = {
        AmazonEBSCSIDriverPolicy = "arn:aws:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy"
      }

      tags = {
        Project     = "runners-eks"
        Environment = var.environment
        Context     = local.cluster_name
      }
    }
  }

  manage_aws_auth_configmap = false
  create_aws_auth_configmap = false

  kms_key_owners         = local.kms_users
  kms_key_administrators = local.kms_users

  aws_auth_users = [
    for eksusr in local.eks_users :
    {
      groups   = ["system:masters", "cluster-admin"]
      userarn  = eksusr[0]
      username = eksusr[1]
    }
  ]

  tags = {
    Project                  = "runners-eks"
    Environment              = var.environment
    Context                  = local.cluster_name
    "karpenter.sh/discovery" = local.cluster_name
  }
}
