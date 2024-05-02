resource "aws_iam_user" "loki" {
  name = "loki-s3-access"
  path = "/system/"

  tags = {
    tag-key = "tag-value"
  }
}

resource "aws_iam_access_key" "loki" {
  user = aws_iam_user.loki.name
}

data "aws_iam_policy_document" "loki_s3_access" {
  statement {
    effect    = "Allow"
    actions   = ["s3:*"]
    resources = [
        aws_s3_bucket.loki_admin.arn,
        aws_s3_bucket.loki_chunks.arn,
        aws_s3_bucket.loki_ruler.arn,
    ]
  }
}

resource "aws_iam_user_policy" "loki_s3_access" {
  name   = "test"
  user   = aws_iam_user.loki.name
  policy = data.aws_iam_policy_document.loki_s3_access.json
}
