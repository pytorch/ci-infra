terraform {
  required_version = ">= 1.7"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  # Backend configured at init time via -backend-config flags.
  # Each cluster gets its own state key:
  #   tofu init -backend-config="bucket=ciforge-tfstate-<cluster>" \
  #             -backend-config="key=<cluster>/base/terraform.tfstate" \
  #             -backend-config="region=us-west-2" \
  #             -backend-config="dynamodb_table=ciforge-terraform-locks"
  backend "s3" {
    encrypt = true
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project   = "ciforge"
      Cluster   = var.cluster_name
      ManagedBy = "opentofu"
    }
  }
}
