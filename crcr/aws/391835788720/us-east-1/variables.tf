variable "github_app_id" {
  description = "GitHub App ID"
  type        = string
}

variable "secret_name" {
  description = "Secrets Manager secret name holding GitHub App credentials"
  type        = string
}

variable "upstream_repo" {
  description = "GitHub upstream repository in owner/repo format"
  type        = string
  default     = "pytorch/pytorch"
}

variable "allowlist_url" {
  description = "GitHub URL to the relay whitelist YAML"
  type        = string
  default     = "https://github.com/pytorch/pytorch/blob/main/.github/allowlist.yml"
}

variable "allowlist_ttl" {
  description = "Whitelist cache TTL in Redis (seconds)"
  type        = number
  default     = 1200
}

variable "environment" {
  description = "Environment name for resource tagging and naming"
  type        = string
  default     = "crcr-prod"
}

variable "vpc_cidr_block" {
  description = "CIDR block for the VPC"
  type        = string
  default     = "10.0.0.0/16"
}

variable "availability_zone_suffixes" {
  description = "Availability zone letter suffixes"
  type        = list(string)
  default     = ["a", "b"]
}
