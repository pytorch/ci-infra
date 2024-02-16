terraform {
  required_version = ">= 1.5"
  required_providers {
    random = {
      source  = "hashicorp/random"
      version = ">= 3.4"
    }
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.5"
    }
    external = {
      source  = "hashicorp/external"
      version = ">= 2.3"
    }
  }
}
