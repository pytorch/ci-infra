locals {
  webhook_zip = abspath("../../../assets/lambdas-download/cross-repo-ci-webhook.zip")
}

resource "aws_lambda_function" "cross_repo_ci_webhook" {
  function_name = "cross_repo_ci_webhook"
  role          = aws_iam_role.cross_repo_ci_relay.arn

  runtime          = "python3.10"
  handler          = "lambda_function.lambda_handler"
  filename         = local.webhook_zip
  source_code_hash = filebase64sha256(local.webhook_zip)

  timeout     = 900
  memory_size = 512

  environment {
    variables = {
      GITHUB_APP_ID         = var.github_app_id
      REDIS_ENDPOINT        = aws_elasticache_replication_group.redis.primary_endpoint_address
      REDIS_LOGIN           = ""
      SECRET_STORE_ARN      = local.secret_store_arn
      UPSTREAM_REPO         = var.upstream_repo
      ALLOWLIST_URL         = var.allowlist_url
      ALLOWLIST_TTL_SECONDS = tostring(var.allowlist_ttl)
    }
  }

  vpc_config {
    security_group_ids = [aws_security_group.redis.id]
    subnet_ids         = module.crcr_vpc.private_subnets
  }
}

resource "aws_lambda_function_url" "cross_repo_ci_webhook" {
  function_name      = aws_lambda_function.cross_repo_ci_webhook.function_name
  authorization_type = "NONE"
}

resource "aws_lambda_permission" "cross_repo_ci_webhook_public" {
  statement_id           = "FunctionUrlAllowPublic"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = aws_lambda_function.cross_repo_ci_webhook.function_name
  principal              = "*"
  function_url_auth_type = "NONE"
}

resource "aws_lambda_permission" "allow_base_invoke" {
  statement_id  = "AllowInvokeFromUrl"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.cross_repo_ci_webhook.function_name
  principal     = "*"
}
