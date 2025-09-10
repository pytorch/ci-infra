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

variable "cert_manager_email" {
  type        = string
  description = "Email to be used for the cert-manager cluster issuer"
  default     = "hostmaster+pytorch-ci-argocd@linuxfoundation.org"
}

variable "letsencrypt_issuer" {
  type        = string
  description = "Name of the cert-manager cluster issuer"
  default     = "letsencrypt-prod"
}

variable "argocd_ingress_host" {
  type        = string
  description = "Public ArgoCD endpoint"
  default     = "argocd.pytorch.org"
}
