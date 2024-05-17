# Auto-scaler lambda instances
variable "ali_aws_region" {
  description = "List of aws region for EC2 runners"
  type        = list
  default     = ["us-east-1"]
}

variable "ali_canary_environment" {
  description = "canary environment prefix"
  type        = string
  default     = "ghci-lf-c"
}

variable "ali_prod_environment" {
  description = "production environment prefix"
  type        = string
  default     = "ghci-lf"
}

variable "aws_vpc_suffixes" {
  description = "suffixes to define aws vpcs per AZ per location"
  type        = list
  default     = ["I", "II"]
}

variable "aws_vpc_suffixes_combinations" {
  description = "this should be the unique combination pair of aws_vpc_suffixes"
  type        = list
  default     = [["I", "II"]]
}

variable "aws_canary_vpc_suffixes" {
  description = "suffixes to define aws vpcs per AZ per location for canary"
  type        = list
  default     = ["I"]
}
