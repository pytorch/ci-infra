resource "aws_s3_bucket_versioning" "terraform_state" {
  count =  data.external.terraform_state_bucket_exists.result.exists == "true" ? 0 : 1
  bucket = local.terraform_state_bucket_name

  versioning_configuration {
    status = "Enabled"
  }

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_public_access_block" "access_terraform_state" {
  count =  data.external.terraform_state_bucket_exists.result.exists == "true" ? 0 : 1
  bucket = local.terraform_state_bucket_name

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
