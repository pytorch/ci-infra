terraform {
  required_version = ">= 1.2"
  required_providers {
    random = {
      version = ">= 3.4"
      source  = "hashicorp/random"
    }
    aws    = {
      version = ">= 5.95, < 6.0"
      source  = "hashicorp/aws"
    }
    kubernetes = {
      version = ">= 2.37, < 3.0"
      source  = "hashicorp/kubernetes"
    }
    external = {
      source  = "hashicorp/external"
      version = "~> 2.0"
    }
    helm = {
      source  = "hashicorp/helm"
      version = "~> 2.10"
    }
    null = {
      source  = "hashicorp/null"
      version = "~> 3.2"
    }
  }
}

provider "kubernetes" {
  host                   = data.terraform_remote_state.runners[0].outputs.cluster_endpoint
  cluster_ca_certificate  = base64decode(data.terraform_remote_state.runners[0].outputs.cluster_ca_certificate)
  
  exec {
    api_version = "client.authentication.k8s.io/v1beta1"
    command     = "aws"
    args = [
      "eks",
      "get-token",
      "--cluster-name",
      data.terraform_remote_state.runners[0].outputs.cluster_name,
    ]
  }
}


provider "helm" {
  kubernetes {
    host                  = data.terraform_remote_state.runners[0].outputs.cluster_endpoint
    cluster_ca_certificate = base64decode(data.terraform_remote_state.runners[0].outputs.cluster_ca_certificate)
    
    exec {
      api_version = "client.authentication.k8s.io/v1beta1"
      command     = "aws"
      args = [
        "eks",
        "get-token",
        "--cluster-name",
        data.terraform_remote_state.runners[0].outputs.cluster_name,
      ]
    }
  }
}
