variable "aws_region" {
  description = "AWS region for the EKS cluster"
  type        = string
}

variable "cluster_name" {
  description = "Name of the EKS cluster"
  type        = string
}

variable "cluster_version" {
  description = "Kubernetes version for EKS cluster"
  type        = string
  default     = "1.35"
}

variable "ip_family" {
  description = "IP family for pod networking: 'ipv4' or 'ipv6'. Immutable after cluster creation."
  type        = string
  default     = "ipv6"

  validation {
    condition     = contains(["ipv4", "ipv6"], var.ip_family)
    error_message = "ip_family must be 'ipv4' or 'ipv6'."
  }
}

variable "enable_windows_node_group" {
  description = "Enable a Windows GPU node group (requires ip_family = 'ipv4')."
  type        = bool
  default     = false
}

variable "windows_node_count" {
  description = "Number of Windows GPU nodes"
  type        = number
  default     = 0
}

variable "windows_instance_type" {
  description = "Instance type for Windows GPU nodes"
  type        = string
  default     = "g4dn.xlarge"
}

variable "windows_ami_type" {
  description = "EKS Windows AMI type (WINDOWS_FULL_2025_x86_64 or WINDOWS_CORE_2025_x86_64)"
  type        = string
  default     = "WINDOWS_FULL_2025_x86_64"
}

variable "windows_ami_id" {
  description = "Custom AMI ID for Windows GPU nodes. Empty = use the EKS-optimized AMI matching windows_ami_type (driver installed in user data). Set to a pre-baked AMI (NVIDIA driver already installed) to skip the user-data driver install and speed up scale-up."
  type        = string
  default     = ""
}

variable "windows_volume_size" {
  description = "Root volume size (GB) for Windows nodes"
  type        = number
  default     = 200
}

variable "vpc_id" {
  description = "VPC ID where EKS cluster will be deployed"
  type        = string
}

variable "subnet_ids" {
  description = "List of subnet IDs for EKS cluster"
  type        = list(string)
}

variable "enable_irsa" {
  description = "Enable IAM Roles for Service Accounts"
  type        = bool
  default     = true
}

variable "cluster_endpoint_private_access" {
  description = "Enable private API server endpoint"
  type        = bool
  default     = true
}

variable "cluster_endpoint_public_access" {
  description = "Enable public API server endpoint"
  type        = bool
  default     = true
}

variable "cluster_endpoint_public_access_cidrs" {
  # EKS public API endpoint is IPv4-only — even on IPv6-only clusters the
  # control-plane public endpoint cannot serve IPv6 clients. Adding ::/0 is
  # meaningless and rejected by EKS. IPv4-only CIDRs only.
  description = "List of IPv4 CIDR blocks that can access the public API server endpoint (EKS public endpoint is IPv4-only)"
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "base_node_count" {
  description = "Fixed number of base infrastructure nodes"
  type        = number
  default     = 5
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
  description = "Maximum percentage of base nodes to update simultaneously (100 = all at once, no drainage)"
  type        = number
  default     = 100
}

variable "base_node_ami_version" {
  description = "EKS-optimized AMI version suffix (e.g. 'v20260318'). Use 'v*' for latest (not recommended). Must update when changing eks_version."
  type        = string
  default     = "v*"

  validation {
    condition     = can(regex("^v([0-9]{8}|\\*)$", var.base_node_ami_version))
    error_message = "base_node_ami_version must be 'v*' or 'vYYYYMMDD' (e.g. 'v20260318')"
  }
}

variable "base_node_subnet_count" {
  description = "Pin the base node group to the first N entries of subnet_ids. 0 = all subnets."
  type        = number
  default     = 0
}

variable "cluster_log_types" {
  description = "EKS control-plane log types shipped to CloudWatch. Empty list disables control-plane logging."
  type        = list(string)
  default     = ["api", "audit", "authenticator", "controllerManager", "scheduler"]
}

variable "enable_secrets_encryption" {
  description = "Enable KMS envelope encryption for Kubernetes secrets at rest"
  type        = bool
  default     = true
}

variable "authentication_mode" {
  description = "EKS cluster authentication mode (API, API_AND_CONFIG_MAP, or CONFIG_MAP)"
  type        = string
  default     = "CONFIG_MAP"
}

variable "bootstrap_cluster_creator_admin_permissions" {
  description = "Whether to grant cluster creator admin permissions (immutable after creation)"
  type        = bool
  default     = true
}

variable "cluster_admin_role_names" {
  description = "Comma-separated IAM role names to grant EKS cluster admin access via access entries (requires API or API_AND_CONFIG_MAP authentication mode)"
  type        = string
  default     = ""
}

variable "tags" {
  description = "Tags to apply to all resources"
  type        = map(string)
  default     = {}
}
