provider "aws" {
  region = var.aws_region
}

locals {
  terraform_state_bucket_name = "${var.bucket_state_name}-${var.project}-${var.environment}"
}
