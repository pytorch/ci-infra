variable "server_addr" {
    description = "The URL of the ArgoCD server"
    type        = string
}

variable "token" {
    description = "The Token for ArgoCD server"
    type        = string
}

variable "organization" {
    description = "The Organization for in the format <org>"
    type        = string
    default     = "lf"
}

variable "namespace" {
    description = "The namespace for in the format <org>-<cloud>"
    type        = string
    default     = "lf-aws"
}

variable "cluster" {
    description = "The name of the cluster as defined in ArgoCD"
    type        = string
    default     = "local"
}

variable "provider_path" {
    description = "The path that contains the cluster folder in the format argocd/cloud/tenant/region"
    type        = string
    default     = "argocd/aws/391835788720/us-east-1"
}

variable "git_revision" {
  description = "The git revision used by ArgoCD git generator"
  type        = string
  default     = "main"
}


variable "aws_secret_name" {
    description = "The name of the AWS SM secret with app id and installion id"
    type        = string
    default     = "pytorch-arc-github-app"
}

variable "aws_secret_key_name" {
    description = "The name of the AWS SM secret with private key"
    type        = string
    default     = "pytorch-arc-github-app-private-key"
}
