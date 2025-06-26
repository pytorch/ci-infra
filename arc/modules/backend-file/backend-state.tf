module "backend-state" {
  source      = "../../../modules/backend-state"

  aws_region  = "#AWS_REGION"
  environment = "prod"
  project     = "pyt-arc"
}
