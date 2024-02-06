variable "environment" {
  description = "environment prefix"
  type        = string
}

variable "vpc_id" {
  description = "The VPC ID to create the cluster in"
  type        = string
}

variable "subnet_ids" {
  description = "The subnet IDs to create the cluster in"
  type        = list(string)
}

variable "aws_vpc_suffix" {
  description = "suffixes to define aws vpcs per AZ per location"
  type        = string
}

variable "eks_cidr_blocks" {
  description = "CIDR blocks to allow access to the EKS cluster"
  type        = list(string)
}

variable "aws_account_id" {
  description = "AWS account ID"
  type        = string
  default     = "308535385114"
}

variable "basic_instance_type" {
  description = "The instance type used to run basic cluster services, such as karpenter and arc coordinator"
  type        = string
  default     = "m5n.4xlarge"
}

variable "additional_kms_users" {
  description = "Additional users to add to the KMS key policy"
  type        = list(string)
  default     = []
}

variable "additional_eks_users" {
  description = "Additional users to add to the EKS cluster"
  type        = list(string)
  default     = []
}

variable "github_app_id" {
  description = "GitHub App ID"
  type        = string
}

variable "github_app_installation_id" {
  description = "GitHub App Installation ID"
  type        = string
}

variable "github_app_private_key" {
  description = "GitHub App Private Key"
  type        = string
}
