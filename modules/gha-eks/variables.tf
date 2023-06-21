variable "environment" {
  description = "environment prefix"
  type        = string
}

variable "aws_region" {
  description = "The AWS region for lambdas and main infra"
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
