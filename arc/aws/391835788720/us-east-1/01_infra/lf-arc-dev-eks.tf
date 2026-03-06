# This is a dev EKS cluster for MultiCloud WG experimentation. Do not expect
# any data stored on this system to persist and configuration could change
# at any time.

module "pytorch_arc_dev_eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "~> 20.37"

  cluster_name    = var.arc_dev_environment
  cluster_version = "1.35"

  cluster_endpoint_public_access = true
  enable_cluster_creator_admin_permissions = false

  kms_key_administrators = [
    "arn:aws:iam::391835788720:role/ossci_gha_terraform"
  ]

  access_entries = {
    ossci_gha_terraform = {
      principal_arn = "arn:aws:iam::${local.aws_account_id}:role/ossci_gha_terraform"

      policy_associations = {
        cluster_admin_policy = {
          policy_arn = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"
          access_scope = {
            namespaces = []
            type       = "cluster"
          }
        }
      }
    }
    pytorch_ci_admins = {
      principal_arn = aws_iam_role.pytorch_ci_admins.arn

      policy_associations = {
        cluster_admin_policy = {
          policy_arn = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"
          access_scope = {
            namespaces = []
            type       = "cluster"
          }
        }
      }
    }
  }

  cluster_compute_config = {
    enabled    = true
    node_pools = ["general-purpose"]
  }

  vpc_id     = module.arc_runners_vpc.vpc_id
  subnet_ids = module.arc_runners_vpc.private_subnets

  tags = {
    Environment = var.arc_dev_environment
  }
}
