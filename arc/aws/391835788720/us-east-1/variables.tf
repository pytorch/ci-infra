# ARC
variable "arc_canary_environment" {
  description = "canary environment prefix"
  type        = string
  default     = "ghci-arc-c"
}

variable "arc_vanguard_environment" {
  description = "vangard environment prefix"
  type        = string
  default     = "ghci-arc-v"
}

variable "arc_prod_environment" {
  description = "production environment prefix"
  type        = string
  default     = "ghci-arc"
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
  default     = ["I", "II", "III"]
}
