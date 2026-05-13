variable "name" {
  description = "Name prefix for VPC resources"
  type        = string
}

variable "cidr" {
  description = "CIDR block for VPC"
  type        = string
  default     = "10.0.0.0/16"
}

variable "azs" {
  description = "Availability zones"
  type        = list(string)
}

variable "private_subnets" {
  description = "CIDR blocks for private subnets"
  type        = list(string)
}

variable "public_subnets" {
  description = "CIDR blocks for public subnets"
  type        = list(string)
}

variable "enable_nat_gateway" {
  description = "Enable NAT gateway for private subnets"
  type        = bool
  default     = true
}

variable "single_nat_gateway" {
  description = "Use a single NAT gateway for all private subnets"
  type        = bool
  default     = false
}

variable "enable_dns_hostnames" {
  description = "Enable DNS hostnames in the VPC"
  type        = bool
  default     = true
}

variable "enable_dns_support" {
  description = "Enable DNS support in the VPC"
  type        = bool
  default     = true
}

variable "tags" {
  description = "Tags to apply to all resources"
  type        = map(string)
  default     = {}
}

variable "private_subnet_tags" {
  description = "Additional tags to apply to private subnets"
  type        = map(string)
  default     = {}
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
