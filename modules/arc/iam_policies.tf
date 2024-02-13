resource "aws_iam_user" "internal_docker_registry_usr" {
  name = "internal_docker_registry-${var.environment}-${var.aws_vpc_suffix}"

  tags = {
    Project                  = "runners-eks"
    Environment              = var.environment
    Context                  = local.cluster_name
  }
}

resource "aws_iam_access_key" "internal_docker_registry_usr_key" {
  user = aws_iam_user.internal_docker_registry_usr.name
}

data "aws_iam_policy_document" "internal_docker_registry_usr_pol_doc" {
  statement {
    effect    = "Allow"
    actions   = ["*"]
    resources = [
        aws_s3_bucket.internal_docker_registry.arn,
        "${aws_s3_bucket.internal_docker_registry.arn}/*"
    ]
  }
}

resource "aws_iam_user_policy" "internal_docker_registry_usr_pol_attach" {
  name   = "internal_docker_registry_policy-${var.environment}-${var.aws_vpc_suffix}"
  user   = aws_iam_user.internal_docker_registry_usr.name
  policy = data.aws_iam_policy_document.internal_docker_registry_usr_pol_doc.json
}
