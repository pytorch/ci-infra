locals {
  webhook_zip = abspath("../assets/lambdas-download/cross-repo-ci-webhook.zip")
}

resource "aws_security_group" "lambda" {
  name        = "crcr-lambda-sg-${var.environment}"
  description = "Security group for Lambda function"
  vpc_id      = module.crcr_vpc.vpc_id
  tags        = local.tags
}

resource "aws_security_group_rule" "lambda_to_redis" {
  type                     = "egress"
  from_port                = 6379
  to_port                  = 6379
  protocol                 = "tcp"
  security_group_id        = aws_security_group.lambda.id
  source_security_group_id = aws_security_group.redis.id
  description              = "Allow Redis access"
}

resource "aws_security_group_rule" "lambda_to_https" {
  type              = "egress"
  from_port         = 443
  to_port           = 443
  protocol          = "tcp"
  security_group_id = aws_security_group.lambda.id
  cidr_blocks       = ["0.0.0.0/0"]
  description       = "Allow HTTPS for Secrets Manager and GitHub API"
}

resource "aws_lambda_function" "webhook" {
  function_name = "crcr-webhook-${var.environment}"
  role          = aws_iam_role.lambda.arn

  runtime          = "python3.13"
  handler          = "lambda_function.lambda_handler"
  filename         = local.webhook_zip
  source_code_hash = filebase64sha256(local.webhook_zip)

  timeout                        = 60
  memory_size                    = 512
  tags                           = local.tags

  environment {
    variables = {
      GITHUB_APP_ID         = var.github_app_id
      REDIS_ENDPOINT        = aws_elasticache_replication_group.redis.primary_endpoint_address
      SECRET_STORE_ARN      = local.secret_store_arn
      UPSTREAM_REPO         = var.upstream_repo
      ALLOWLIST_URL         = var.allowlist_url
      ALLOWLIST_TTL_SECONDS = tostring(var.allowlist_ttl)
    }
  }

  vpc_config {
    security_group_ids = [aws_security_group.lambda.id]
    subnet_ids         = module.crcr_vpc.private_subnets
  }
}

resource "aws_cloudwatch_log_group" "webhook" {
  name              = "/aws/lambda/${aws_lambda_function.webhook.function_name}"
  retention_in_days = 90
  tags              = local.tags
}

resource "aws_lambda_function_url" "webhook" {
  function_name      = aws_lambda_function.webhook.function_name
  authorization_type = "NONE"
}

# Starting Oct 2025, Lambda function URLs require both lambda:InvokeFunctionUrl
# and lambda:InvokeFunction permissions.
# See: https://docs.aws.amazon.com/lambda/latest/dg/urls-auth.html
resource "aws_lambda_permission" "webhook_function_url_invoke" {
  function_name          = aws_lambda_function.webhook.function_name
  action                 = "lambda:InvokeFunctionUrl"
  principal              = "*"
  function_url_auth_type = "NONE"
}

resource "aws_lambda_permission" "webhook_function_invoke" {
  function_name             = aws_lambda_function.webhook.function_name
  action                    = "lambda:InvokeFunction"
  principal                 = "*"
  invoked_via_function_url  = true
}
