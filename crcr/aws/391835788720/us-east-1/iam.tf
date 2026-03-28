resource "aws_iam_role" "cross_repo_ci_relay" {
  name = "cross-repo-ci-relay-lambda-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "cross_repo_ci_relay_basic_exec" {
  role       = aws_iam_role.cross_repo_ci_relay.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy_attachment" "cross_repo_ci_relay_vpc_exec" {
  role       = aws_iam_role.cross_repo_ci_relay.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
}

resource "aws_iam_role_policy" "cross_repo_ci_relay_secrets" {
  name = "cross-repo-ci-relay-secrets-access"
  role = aws_iam_role.cross_repo_ci_relay.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["secretsmanager:GetSecretValue"]
      Resource = [local.secret_store_arn]
    }]
  })
}
