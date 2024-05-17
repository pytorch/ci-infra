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
