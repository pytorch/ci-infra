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

variable "ami_filter_linux" {
  description = "AMI for linux"
  type        = list
  default     = ["amzn2-ami-hvm-2.0.20240306.2-x86_64-ebs"]
}

variable "ami_filter_linux_arm64" {
  description = "AMI for linux"
  type        = list
  default     = ["al2023-ami-2023.5.202*-kernel-6.1-arm64"]
}

variable "ami_filter_windows" {
  description = "AMI for windows"
  type        = list
  default     = ["Windows 2019 GHA CI - 20240830161839"]
}
