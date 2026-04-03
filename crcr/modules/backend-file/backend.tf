terraform {
  backend "s3" {
    bucket         = "tfstate-pyt-crcr-prod"
    key            = "crcr/terraform.tfstate"
    region         = "#AWS_REGION"
    dynamodb_table = "tfstate-lock-pyt-crcr-prod"
  }
}
