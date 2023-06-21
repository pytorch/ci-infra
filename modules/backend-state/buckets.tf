resource "aws_s3_bucket" "terraform_state" {
  count =  data.external.terraform_state_bucket_exists.result.exists == "true" ? 0 : 1
  bucket = local.terraform_state_bucket_name

  versioning {
    enabled = true
  }

  lifecycle {
    prevent_destroy = true
  }
}
