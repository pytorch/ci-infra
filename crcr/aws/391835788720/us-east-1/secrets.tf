resource "aws_secretsmanager_secret" "main" {
  name = "${var.environment}-crcr-secret"
  tags = local.tags
}

resource "aws_secretsmanager_secret_version" "main" {
  secret_id = aws_secretsmanager_secret.main.id
  secret_string = jsonencode({
    GITHUB_APP_SECRET      = var.github_app_secret
    GITHUB_APP_PRIVATE_KEY = var.github_app_privatekey
    REDIS_LOGIN            = random_password.redis_password.result
  })
}
