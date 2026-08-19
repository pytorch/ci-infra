output "sigv4_proxy_role_arn" {
  description = "IRSA role ARN for the sigv4-proxy service account (read-only AWS + Bedrock invoke)"
  value       = aws_iam_role.sigv4_proxy.arn
}
