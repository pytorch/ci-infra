locals {
  result_zip = abspath("../assets/lambdas-download/cross-repo-ci-result.zip")
}

resource "aws_lambda_function" "result" {
  function_name = "crcr-result-${var.environment}"
  role          = aws_iam_role.lambda.arn

  runtime          = "python3.13"
  handler          = "lambda_function.lambda_handler"
  filename         = local.result_zip
  source_code_hash = filebase64sha256(local.result_zip)

  timeout                        = 60
  memory_size                    = 512
  reserved_concurrent_executions = 50
  tags                           = local.tags

  environment {
    variables = {
      GITHUB_APP_ID         = var.github_app_id
      REDIS_ENDPOINT        = aws_elasticache_replication_group.redis.primary_endpoint_address
      SECRET_STORE_ARN      = local.secret_store_arn
      UPSTREAM_REPO         = var.upstream_repo
      ALLOWLIST_URL         = var.allowlist_url
      ALLOWLIST_TTL_SECONDS = tostring(var.allowlist_ttl)
      HUD_API_URL           = var.hud_api_url
    }
  }

  vpc_config {
    security_group_ids = [aws_security_group.lambda.id]
    subnet_ids         = module.crcr_vpc.private_subnets
  }
}

resource "aws_cloudwatch_log_group" "result" {
  name              = "/aws/lambda/${aws_lambda_function.result.function_name}"
  retention_in_days = 90
  tags              = local.tags
}

resource "aws_lambda_function_url" "result" {
  function_name      = aws_lambda_function.result.function_name
  authorization_type = "NONE"
}

resource "aws_lambda_permission" "result_function_url_invoke" {
  function_name          = aws_lambda_function.result.function_name
  action                 = "lambda:InvokeFunctionUrl"
  principal              = "*"
  function_url_auth_type = "NONE"
}

resource "aws_lambda_permission" "result_function_invoke" {
  function_name            = aws_lambda_function.result.function_name
  action                   = "lambda:InvokeFunction"
  principal                = "*"
  invoked_via_function_url = true
}
