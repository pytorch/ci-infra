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
    external = {
      source  = "hashicorp/external"
      version = "~> 2.0"
    }
    kubernetes = {
      version = ">= 2.37, < 3.0"
      source  = "hashicorp/kubernetes"
    }
    argocd = {
      source  = "argoproj-labs/argocd"
      version = "~>7.11"
    }
  }
}

# Handle the case of terraform state for helm existing or not including cluster_info yet
locals {
  helm_exists              = data.external.helm_bucket_exists.result.exists == "true"
  cluster_info             = local.helm_exists ? try(data.terraform_remote_state.helm[0].outputs.cluster_info, null) : null
  argocd_admin_secret_name = local.helm_exists ? try(data.terraform_remote_state.helm[0].outputs.argocd_admin_secret_name, null) : null
  argocd_endpoint          = local.helm_exists ? try(data.terraform_remote_state.helm[0].outputs.argocd_endpoint, null) : null
  argocd_ready             = local.argocd_endpoint != null && local.argocd_admin_secret_name != null
}

provider "kubernetes" {
  host                   = local.cluster_info != null ? local.cluster_info.endpoint : null
  cluster_ca_certificate  = local.cluster_info != null ? local.cluster_info.ca_certificate : null
  
  dynamic "exec" {
    for_each = local.cluster_info != null ? [1] : []
    content {
      api_version = "client.authentication.k8s.io/v1beta1"
      command     = "aws"
      args = [
        "eks",
        "get-token",
        "--cluster-name",
        local.cluster_info.name,
      ]
    }
  }
}

data "kubernetes_secret_v1" "argocd_initial_admin_secret" {
  count = local.argocd_admin_secret_name != null ? 1 : 0
  metadata {
    name      = local.argocd_admin_secret_name
    namespace = "argocd"
  }
}

provider "argocd" {
  server_addr = local.argocd_ready ? local.argocd_endpoint : null
  username    = "admin"
  password    = local.argocd_ready ? data.kubernetes_secret_v1.argocd_initial_admin_secret[0].data.password : null
}
