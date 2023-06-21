provider "aws" {
  region = "us-east-1"
}

# There is an issue with kubernetes provider to get authenticaiton with EKS cluster
# so it is needed to instruct it the endpoint and authentication
# this is problematic and prevents the creation of multiple eks clusters
# so, until there is a solution for this, we won't be supporting multiple eks clusters
# including a canary environment for pytorch-canary
data "aws_eks_cluster" "prod_eks" {
  name = "${var.prod_environment}-runners-eks-${var.aws_vpc_suffixes[0]}"
}

data "aws_eks_cluster_auth" "prod_eks" {
  name = "${var.prod_environment}-runners-eks-${var.aws_vpc_suffixes[0]}"
}

provider "kubernetes" {
  host                   = data.aws_eks_cluster.prod_eks.endpoint
  cluster_ca_certificate = base64decode(data.aws_eks_cluster.prod_eks.certificate_authority[0].data)
  token                  = data.aws_eks_cluster_auth.prod_eks.token
}
