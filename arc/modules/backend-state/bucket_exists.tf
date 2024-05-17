data "external" "terraform_state_bucket_exists" {
  program = ["bash", "-c", "aws s3api head-bucket --bucket ${local.terraform_state_bucket_name} >/dev/null 2>&1 && echo '{\"exists\": \"true\"}' || echo '{\"exists\": \"false\"}'"]
}
