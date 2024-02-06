variable "canary_environment" {
  description = "canary environment prefix"
  type        = string
  default     = "ghci-arc-c"
}

variable "vanguard_environment" {
  description = "vangard environment prefix"
  type        = string
  default     = "ghci-arc-v"
}

variable "prod_environment" {
  description = "production environment prefix"
  type        = string
  default     = "ghci-arc"
}

variable "aws_vpc_suffixes" {
  description = "suffixes to define aws vpcs per AZ per location"
  type        = list
  default     = ["I", "II"]
}

variable "aws_canary_vpc_suffixes" {
  description = "suffixes to define aws vpcs per AZ per location for canary"
  type        = list
  default     = ["I"]
}

variable "GITHUB_APP_ID" {
  description = "Passed in via TF_VAR_GITHUB_APP_ID"
  type        = string
}

variable "GITHUB_APP_INSTALLATION_ID" {
  description = "Passed in via TF_VAR_GITHUB_APP_INSTALLATION_ID"
  type        = string
}

variable "GITHUB_APP_PRIVATE_KEY" {
  description = "Passed in via TF_VAR_GITHUB_APP_PRIVATE_KEY"
  type        = string
}

variable "GITHUB_APP_PRIVATE_KEY_CANARY" {
  description = "Passed in via TF_VAR_GITHUB_APP_PRIVATE_KEY"
  type        = string
}
