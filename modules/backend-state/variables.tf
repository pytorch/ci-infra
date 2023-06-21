variable "aws_region" {
  description = "The AWS region for lambdas and main infra"
  type        = string
}

variable "bucket_state_name" {
  description = "The naem for the bucket state"
  type        = string
  default     = "tfstate"
}

variable "dynamo_table_name" {
  description = "value for dynamo table name"
  type        = string
  default     = "tfstate-lock"
}

variable "project" {
  description = "Name for the project"
  type        = string
}

variable "environment" {
  description = "Name for the environment"
  type        = string
}
