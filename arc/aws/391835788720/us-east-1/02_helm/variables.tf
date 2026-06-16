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

variable "argocd_sa_terraform" {
  type        = string
  description = "Name of the ArgoCD service account for terraform"
  default     = "terraform-sa"
}

# ─── gharts variables ──────────────────────────────────────────────────────────

variable "gharts_chart_version" {
  type        = string
  description = "gharts Helm chart version"
  default     = "0.0.6"
}

variable "gharts_namespace" {
  type        = string
  description = "Namespace for gharts installation"
  default     = "gharts"
}

variable "gharts_ingress_host" {
  type        = string
  description = "Public gharts endpoint"
  default     = "gharts.pytorch.org"
}

variable "gharts_github_org" {
  type        = string
  description = "GitHub organization managed by gharts"
  default     = "pytorch"
}

variable "gharts_oidc_issuer" {
  type        = string
  description = "OIDC issuer URL"
  default     = "https://sso.linuxfoundation.org"
}

variable "gharts_oidc_audience" {
  type        = string
  description = "OIDC audience expected in tokens"
  default     = "https://gharts.pytorch.org/api"
}

variable "gharts_oidc_jwks_url" {
  type        = string
  description = "JWKS endpoint for OIDC token validation; defaults to <issuer>/.well-known/jwks.json"
  default     = ""
}

variable "gharts_oidc_client_id" {
  type        = string
  description = "OIDC SPA client ID"
  default     = "gFWBLBdbhBPCdkLF82qcRWlFUGMI8icP"
}
