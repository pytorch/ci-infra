variable "argocd_version" {
  description = "ArgoCD Helm chart version"
  type        = string
  default     = "8.0.0"
}

variable "argocd_namespace" {
  description = "Namespace for ArgoCD installation"
  type        = string
  default     = "argocd"
}

variable "argocd_ingress_host" {
  type        = string
  description = "Public ArgoCD endpoint"
  default     = "argocd.pytorch.org"
}

variable "letsencrypt_issuer" {
  type        = string
  description = "Name of the cert-manager cluster issuer"
  default     = "letsencrypt-prod"
}

variable "argocd_dex_github_org" {
    type        = string
    description = "GitHub org used for ArgoCD Dex GitHub connector"
    default     = "pytorch-fdn"
}

variable "argocd_dex_github_team" {
    type        = string
    description = "GitHub team with readonly access to ArgoCD"
    default     = "multicloud-wg"
}

variable "argocd_dex_github_client_id" {
    type        = string
    description = "GitHub OAuth App Client Id ArgoCD Dex"
}

variable "argocd_dex_github_client_secret" {
    type        = string
    description = "GitHub OAuth App Client Id ArgoCD Dex"
}
