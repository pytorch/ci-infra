# All variables are passed via -var flags from the justfile,
# which reads values from clusters.yaml.

variable "cluster_name" {
  description = "EKS cluster name (e.g. pytorch-arc-staging)"
  type        = string
}

variable "aws_region" {
  description = "AWS region for the cluster"
  type        = string
}

variable "vpc_cidr" {
  description = "VPC CIDR block"
  type        = string
  default     = "10.0.0.0/16"
}

variable "single_nat_gateway" {
  description = "Use a single NAT gateway (cost saving) vs one per AZ (HA)"
  type        = bool
  default     = false
}

variable "base_node_count" {
  description = "Number of base infrastructure nodes (fixed, tainted CriticalAddonsOnly)"
  type        = number
  default     = 3
}

variable "base_node_instance_type" {
  description = "Instance type for base infrastructure nodes"
  type        = string
  default     = "m5.xlarge"
}

variable "base_node_max_unavailable_percentage" {
  description = "Max unavailable percentage during node group updates"
  type        = number
  default     = 33
}

variable "base_node_ami_version" {
  description = "EKS-optimized AMI version suffix (e.g. 'v20260318'). Use 'v*' for latest (not recommended)."
  type        = string
  default     = "v*"
}

variable "eks_version" {
  description = "EKS Kubernetes version"
  type        = string
  default     = "1.35"
}

variable "authentication_mode" {
  description = "EKS cluster authentication mode (API, API_AND_CONFIG_MAP, or CONFIG_MAP)"
  type        = string
  default     = "CONFIG_MAP"
}

variable "cluster_admin_role_names" {
  description = "Comma-separated IAM role names to grant EKS cluster admin access via access entries"
  type        = string
  default     = ""
}
