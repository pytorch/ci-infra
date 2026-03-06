# Handle the case of "terraform plan" on all layers before the
# state for a previous layer has been created (which happens on
# the very first run if more than one layer is added).
data "external" "#BACKEND_KEY_bucket_exists" {
  program = ["bash", "-c", "aws s3api head-object --bucket tfstate-pyt-arc-prod --key #BACKEND_KEY/terraform.tfstate >/dev/null 2>&1 && echo '{\"exists\": \"true\"}' || echo '{\"exists\": \"false\"}'"]
}

data "terraform_remote_state" "#BACKEND_KEY" {
  count =  data.external.#BACKEND_KEY_bucket_exists.result.exists == "true" ? 1 : 0
  backend = "s3"
  config = {
    bucket         = "tfstate-pyt-arc-prod"
    key            = "#BACKEND_KEY/terraform.tfstate"
    region         = "#AWS_REGION"
    dynamodb_table = "tfstate-lock-pyt-arc-prod"
  }
}
