output "efs_filesystem_id" {
  description = "EFS filesystem ID for pypi-cache storage"
  value       = aws_efs_file_system.pypi_cache.id
}

output "wants_collector_role_arn" {
  description = "IAM role ARN for the wants-collector service account (IRSA)"
  value       = aws_iam_role.wants_collector.arn
}

output "wheel_syncer_role_arn" {
  description = "IAM role ARN for the wheel-syncer service account (IRSA)"
  value       = aws_iam_role.wheel_syncer.arn
}
