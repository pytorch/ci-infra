provider "aws" {
  region = "us-east-1"
}

data "aws_eks_cluster" "c7g" {
  name = "ghci-runners-c7g-4xl"
}

data "aws_eks_cluster_auth" "c7g" {
  name = "ghci-runners-c7g-4xl"
}

data "aws_eks_cluster" "g4dn" {
  name = "ghci-runners-g4dn-4xl"
}

data "aws_eks_cluster_auth" "g4dn" {
  name = "ghci-runners-g4dn-4xl"
}

provider "kubernetes" {
  #alias = "c7g"
  host                   = data.aws_eks_cluster.c7g.endpoint
  cluster_ca_certificate = base64decode(data.aws_eks_cluster.c7g.certificate_authority[0].data)

  exec {
    api_version = "client.authentication.k8s.io/v1beta1"
    command     = "aws"
    args        = ["eks", "get-token", "--cluster-name", "ghci-runners-c7g-4xl"]
  }
}
/*
provider "kubernetes" {
  alias = "g4dn"
  host                   = data.aws_eks_cluster.g4dn.endpoint
  cluster_ca_certificate = base64decode(data.aws_eks_cluster.g4dn.certificate_authority[0].data)

  exec {
    api_version = "client.authentication.k8s.io/v1beta1"
    command     = "aws"
    args        = ["eks", "get-token", "--cluster-name", "ghci-runners-g4dn-4xl"]
  }
}*/
