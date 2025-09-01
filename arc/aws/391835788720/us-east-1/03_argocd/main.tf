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
    helm = {
      source  = "hashicorp/helm"
      version = "~> 2.10"
    }
    argocd = {
      source  = "argoproj-labs/argocd"
      version = "~>7.11"
    }
  }
}

# The Argo Provider is not configured for now
# We'll need a more secure setup that does not rely on obtaining
# the admin password from the previous layer's state

# provider "argocd" {
#   server_addr = "${data.terraform_remote_state.outputs.argocd_ingress_host}:443"
#   username    = "admin"
#   password    = data.terraform_remote_state.outputs.argocd_ingress_host.password
# }
