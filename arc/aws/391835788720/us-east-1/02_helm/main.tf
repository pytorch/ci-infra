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
  host                   = local.cluster_endpoint
  cluster_ca_certificate  = base64decode(local.cluster_ca_certificate)
  
  exec {
    api_version = "client.authentication.k8s.io/v1beta1"
    command     = "aws"
    args = [
      "eks",
      "get-token",
      "--cluster-name",
      local.cluster_name,
    ]
  }
}


provider "helm" {
  kubernetes {
    host                  = local.cluster_endpoint
    cluster_ca_certificate = base64decode(local.cluster_ca_certificate)
    
    exec {
      api_version = "client.authentication.k8s.io/v1beta1"
      command     = "aws"
      args = [
        "eks",
        "get-token",
        "--cluster-name",
        local.cluster_name,
      ]
    }
  }
}

locals {
  cluster_name            = data.terraform_remote_state.runners[0].outputs.cluster_name
  cluster_endpoint        = data.terraform_remote_state.runners[0].outputs.cluster_endpoint
  cluster_ca_certificate   = data.terraform_remote_state.runners[0].outputs.cluster_ca_certificate
  vpc_id                  = try(data.terraform_remote_state.runners[0].outputs.vpc_id, "")
  oidc_provider_arn       = try(data.terraform_remote_state.runners[0].outputs.oidc_provider_arn, "")
  cluster_oidc_issuer_url = try(data.terraform_remote_state.runners[0].outputs.cluster_oidc_issuer_url, "")
  oidc_provider           = local.cluster_oidc_issuer_url != "" ? replace(local.cluster_oidc_issuer_url, "https://", "") : ""
}
