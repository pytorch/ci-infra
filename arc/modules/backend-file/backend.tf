terraform {
  backend "s3" {
    bucket         = "tfstate-pyt-arc-prod"
    key            = "runners/terraform.tfstate"
    region         = "#AWS_REGION"
    dynamodb_table = "tfstate-lock-pyt-arc-prod"
  }
}
