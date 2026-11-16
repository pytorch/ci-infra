# All variables are passed via -var flags from the justfile,
# which reads values from clusters.yaml.

variable "cluster_name" {
  description = "EKS cluster name (e.g. meta-staging-aws-ue1)"
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

variable "coredns_replicas" {
  description = "Number of CoreDNS replicas (pinned; autoscaling disabled). Per-cluster via clusters.yaml."
  type        = number
  default     = 6

  validation {
    condition     = var.coredns_replicas >= 2
    error_message = "coredns_replicas must be >= 2 (single-replica CoreDNS deadlocks the PDB; zero replicas means no DNS)."
  }
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

# --- Cost knobs for small (dev) clusters. Defaults reproduce the prod shape. ---

variable "nat_secondary_eip_count" {
  description = "Secondary EIPs attached to each NAT gateway (SNAT port capacity at prod scale). 0 for small clusters."
  type        = number
  default     = 7

  validation {
    condition     = var.nat_secondary_eip_count >= 0 && var.nat_secondary_eip_count <= 7
    error_message = "nat_secondary_eip_count must be between 0 and 7 (AWS caps a NAT gateway at 8 IPv4 addresses)."
  }
}

variable "cluster_log_types" {
  description = "Comma-separated EKS control-plane log types shipped to CloudWatch. Empty string disables control-plane logging."
  type        = string
  default     = "api,audit,authenticator,controllerManager,scheduler"
}

variable "base_node_disk_size" {
  description = "Root EBS volume size (GiB) of each base node"
  type        = number
  default     = 100
}

variable "base_node_subnet_count" {
  description = "Pin the base node group to the first N private subnets (AZs). 0 = all subnets. 1 keeps a single base node and its zonal EBS PVCs (Harbor) in one AZ."
  type        = number
  default     = 0

  validation {
    condition     = var.base_node_subnet_count >= 0
    error_message = "base_node_subnet_count must be >= 0."
  }
}
