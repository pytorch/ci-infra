variable "prod_environment" {
  description = "production environment prefix"
  type        = string
  default     = "ghci-arc"
}

variable "aws_region" {
  description = "The AWS region for lambdas and main infra"
  type        = string
  default     = "us-east-1"
}

variable "aws_vpc_suffixes" {
  description = "suffixes to define aws vpcs per AZ per location"
  type        = list
  default     = ["I"]
}

variable "availability_zones" {
  description = "List to specify the availability zones for which subnes on prod environment will be created."
  type        = list
  default     = [
    "us-east-1a",
    "us-east-1b",
    "us-east-1c",
    "us-east-1d",
    # "us-east-1e", currently there is no capacity in this AZ
    "us-east-1f",
  ]
}

variable "availability_zones_canary" {
  description = "List to specify the availability zones for which subnes on canary environment will be created."
  type        = list
  default     = [
    "us-east-1b",
    "us-east-1d",
    "us-east-1f",
  ]
}
