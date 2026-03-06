output "role_arn" {
  description = "ARN of the Karpenter controller IAM role"
  value       = aws_iam_role.karpenter_controller.arn
}

output "role_name" {
  description = "Name of the Karpenter controller IAM role"
  value       = aws_iam_role.karpenter_controller.name
}

output "queue_name" {
  description = "Name of the SQS queue for interruption handling"
  value       = aws_sqs_queue.karpenter.name
}

output "queue_arn" {
  description = "ARN of the SQS queue for interruption handling"
  value       = aws_sqs_queue.karpenter.arn
}

output "queue_url" {
  description = "URL of the SQS queue for interruption handling"
  value       = aws_sqs_queue.karpenter.url
}
