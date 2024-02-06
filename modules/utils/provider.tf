provider "aws" {
  region = local.aws_region
}

provider "kubernetes" {
  config_path = "~/.kube/config"
  config_context = "arn:aws:eks:us-east-1:391835788720:cluster/ghci-arc-c-runners-eks-I"
}

provider "helm" {
  kubernetes {
    config_path = "~/.kube/config"
  }
}
