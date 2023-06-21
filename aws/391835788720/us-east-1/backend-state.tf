module "backend-state" {
  source      = "../../../modules/backend-state"

  aws_region  = "us-east-1"
  environment = "prod"
  project     = "pyt-gha"
}
