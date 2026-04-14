module "backend-state" {
  source      = "../modules/backend-state"

  aws_region  = "#AWS_REGION"
  environment = "#ENVIRONMENT"
  project     = "pyt-crcr"
}
