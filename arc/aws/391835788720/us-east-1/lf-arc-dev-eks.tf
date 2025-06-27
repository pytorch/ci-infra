# This is a dev EKS cluster for MultiCloud WG experimentation. Do not expect
# any data stored on this system to persist and configuration could change
# at any time.

module "pytorch_arc_dev_eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "~> 20.37"

  cluster_name    = "lf-arc-dev"
  cluster_version = "1.33"

  cluster_endpoint_public_access = true

  # This creates permissions for the cluster creator account to manage the
  # cluster. We want this to be the CI user that runs Terraform (ossci_gha_terraform)
  enable_cluster_creator_admin_permissions = true

  cluster_compute_config = {
    enabled    = true
    node_pools = ["general-purpose"]
  }

  vpc_id     = module.arc_runners_vpc.vpc_id
  subnet_ids = module.arc_runners_vpc.private_subnets

  tags = {
    Environment = var.arc_prod_environment
  }
}

################
# EKS - Access #
################

resource "aws_eks_access_entry" "pytorch_arc_dev_eks_admin_role" {
  cluster_name      = module.pytorch_arc_dev_eks.cluster_name
  principal_arn     = aws_iam_role.pytorch_ci_admins.arn
  kubernetes_groups = ["cluster-admins"]
  type              = "STANDARD"
}

resource "kubernetes_cluster_role_binding" "pytorch_arc_dev_eks_admin_binding" {
  provider = kubernetes.lf-arc-dev

  metadata {
    name = "cluster-admins-binding"
  }

  subject {
    kind      = "Group"
    name      = "cluster-admins"
    api_group = "rbac.authorization.k8s.io"
  }

  role_ref {
    kind      = "ClusterRole"
    name      = "cluster-admin"
    api_group = "rbac.authorization.k8s.io"
  }
}

provider "kubernetes" {
  alias = "lf-arc-dev"
  host                   = module.pytorch_arc_dev_eks.cluster_endpoint
  cluster_ca_certificate = base64decode(module.pytorch_arc_dev_eks.cluster_certificate_authority_data)
  exec {
    api_version = "client.authentication.k8s.io/v1beta1"
    command     = "aws"
    args        = ["eks", "get-token", "--cluster-name", module.pytorch_arc_dev_eks.cluster_name]
  }
}
