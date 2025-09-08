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
