terraform {
  required_version = ">= 1.2"
  required_providers {
    random = {
      version = ">= 3.4"
      source  = "hashicorp/random"
    }
    aws    = {
      version = ">= 5.5"
      source  = "hashicorp/aws"
    }
  }
}
