output "agent_role_arn" {
  description = "IRSA role ARN for the sandbox-agent service account (read-only Bedrock invoke)"
  value       = aws_iam_role.agent.arn
}
