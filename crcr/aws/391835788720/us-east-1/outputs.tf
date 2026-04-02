output "webhook_function_url" {
  value       = aws_lambda_function_url.webhook.function_url
  description = "GitHub App webhook URL; configure the GitHub App webhook as <url>/github/webhook"
}

output "redis_endpoint" {
  value       = aws_elasticache_replication_group.redis.primary_endpoint_address
  description = "Redis primary endpoint"
}
