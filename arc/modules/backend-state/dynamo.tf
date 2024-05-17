resource "aws_dynamodb_table" "terraform_state_lock" {
  count =  data.external.terraform_state_bucket_exists.result.exists == "true" ? 0 : 1
  name           = "${var.dynamo_table_name}-${var.project}-${var.environment}"
  read_capacity  = 1
  write_capacity = 1
  hash_key       = "LockID"

  attribute {
    name = "LockID"
    type = "S"
  }
}