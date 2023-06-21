module "eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "~> 19.0"

  cluster_name    = "${var.environment}-runners-eks-${var.aws_vpc_suffix}"
  cluster_version = "1.27"

  cluster_endpoint_public_access  = true

  cluster_security_group_additional_rules = {
    ingress = {
      description           = "To node 1025-65535"
      type                       = "ingress"
      from_port             = 0
      to_port                  = 0
      protocol                = -1
      cidr_blocks           = [
        "129.134.0.0/19",
        "66.220.144.0/20",
        "34.94.18.0/25",
        "35.192.199.128/25",
        "163.114.128.0/20",
        "157.240.128.0/18",
        "102.221.188.0/22",
        "31.13.96.0/19",
        "129.134.96.0/20",
        "18.190.96.139/32",
        "185.60.216.0/22",
        "102.132.112.0/20",
        "129.134.64.0/20",
        "157.240.192.0/18",
        "185.89.216.0/22",
        "35.239.7.131/32",
        "31.13.64.0/19",
        "204.15.20.0/22",
        "157.240.0.0/19",
        "179.60.192.0/22",
        "103.4.96.0/22",
        "69.63.176.0/20",
        "74.119.76.0/22",
        "163.70.128.0/17",
        "157.240.64.0/19",
        "69.171.224.0/19",
        "173.252.64.0/18",
        "129.134.80.0/20",
        "173.252.64.0/22",
        "147.75.208.0/20",
        "199.201.64.0/22",
        "66.111.48.0/22",
        "157.240.32.0/19",
        "163.77.128.0/17",
        "34.82.178.0/25",
        "129.134.32.0/19",
        "31.13.24.0/21",
        "45.64.40.0/22",
        "129.134.128.0/17",
        "102.132.96.0/20",
      ]
      ipv6_cidr_blocks = [
        "64:ff9b::b93c:c00/118",
        "64:ff9b::2d40:0/118",
        "2a03:2880:d100::/40",
        "2a03:2880:c000::/36",
        "64:ff9b::ccf:0/118",
        "64:ff9b::674:400/118",
        "64:ff9b::453f:0/116",
        "64:ff9b::9df0:0/114",
        "64:ff9b::adfc:0/114",
        "64:ff9b::42dc:0/116",
        "2620:0:1c00::/40",
        "64:ff9b::8186:0/113",
        "2a03:2880::/33",
        "64:ff9b::1fd:0/117",
        "2a03:2880:d200::/39",
        "2a03:2880:d400::/38",
        "2a03:2880:8000::/34",
        "2620:10d:c080::/41",
        "64:ff9b::45ab:0/115",
        "2a03:2880:e000::/35",
        "64:ff9b::1fd:0/114",
        "64:ff9b::4a77:400/118",
        "2a03:2880:d800::/37",
        "64:ff9b::b33c:c00/118",
      ]
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

  vpc_id                   = var.vpc_id
  subnet_ids               = var.subnet_ids

  eks_managed_node_group_defaults = {
    instance_types = ["c7g.4xlarge"]
    ami_type       = "AL2_ARM_64"
  }

  eks_managed_node_groups = {
    green = {
      min_size     = 1
      max_size     = 20
      desired_size = 1

      instance_types = ["c7g.4xlarge"]
      ami_type       = "AL2_ARM_64"
      capacity_type  = "SPOT"
      labels = {
        Project     = var.environment
        Environment = "${var.environment}-runners-eks-${var.aws_vpc_suffix}"
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
        Context     = "${var.environment}-runners-eks-${var.aws_vpc_suffix}"
      }
    }
  }

  manage_aws_auth_configmap = true
  create_aws_auth_configmap = false

  tags = {
    Project     = "runners-eks"
    Environment = var.environment
    Context     = "${var.environment}-runners-eks-${var.aws_vpc_suffix}"
  }
}