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
  description = "Cluster identifier in clusters.yaml (e.g. arc-staging)"
  type        = string
}
