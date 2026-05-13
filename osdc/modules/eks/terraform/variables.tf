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

variable "pod_cidr_buckets" {
  description = "Per-(bucket, AZ) secondary /16 CIDR blocks for VPC CNI Custom Networking pod IP allocation. Outer key = bucket name (bucket-1..bucket-4), inner key = AZ name (e.g. us-east-2a), value = /16 CIDR in 100.64.0.0/10 (CGNAT). See INCREASE_IPV4.md PR 4."
  type        = map(map(string))

  validation {
    condition     = length(var.pod_cidr_buckets) > 0
    error_message = "pod_cidr_buckets must be non-empty. See INCREASE_IPV4.md PR 4 for the expected shape."
  }

  validation {
    condition     = alltrue([for bucket_name in keys(var.pod_cidr_buckets) : can(regex("^bucket-[1-4]$", bucket_name))])
    error_message = "pod_cidr_buckets keys must match 'bucket-1' through 'bucket-4'."
  }

  validation {
    condition = alltrue(flatten([
      for bucket_name, az_map in var.pod_cidr_buckets : [
        for az_name, cidr in az_map : can(regex("^100\\.((6[4-9])|(7[0-9])|(8[0-9])|(9[0-9])|(1[01][0-9])|(12[0-7]))\\.0\\.0/16$", cidr))
      ]
    ]))
    error_message = "All pod_cidr_buckets CIDRs must be /16 blocks inside 100.64.0.0/10 (CGNAT) on a /16 boundary (third and fourth octets must be 0)."
  }

  validation {
    condition = length(distinct(flatten([
      for bucket_name, az_map in var.pod_cidr_buckets : [
        for az_name, cidr in az_map : cidr
      ]
      ]))) == length(flatten([
      for bucket_name, az_map in var.pod_cidr_buckets : [
        for az_name, cidr in az_map : cidr
      ]
    ]))
    error_message = "All pod_cidr_buckets CIDRs must be unique (no duplicates across buckets/AZs)."
  }
}
