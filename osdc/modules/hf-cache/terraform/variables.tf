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

# The model-cache bucket name is derived from aws_region
# (pytorch-hf-model-cache-<region>, see locals in main.tf) and provisioned by
# terraform/hf-cache-bucket/, so it needs no variable here.
