variable "cluster_name" {
  description = "EKS cluster name"
  type        = string
}

variable "aws_region" {
  description = "AWS region"
  type        = string
}

variable "state_bucket" {
  description = "S3 bucket for terraform state (used to read base outputs)"
  type        = string
}

variable "cluster_id" {
  description = "Cluster identifier in clusters.yaml (e.g. meta-staging-aws-uw1)"
  type        = string
}

variable "hf_cache_bucket" {
  description = "Shared S3 bucket holding the HuggingFace model cache (plain files). Managed by terraform/hf-cache-bucket/, referenced here only for IAM scoping."
  type        = string
  default     = "pytorch-hf-model-cache"
}
