output "mount_role_arn" {
  description = "IAM role ARN for the hf-cache-mount service account (IRSA, read-only)"
  value       = aws_iam_role.mount.arn
}

output "refresh_role_arn" {
  description = "IAM role ARN for the hf-cache-refresh service account (IRSA, read/write)"
  value       = aws_iam_role.refresh.arn
}

output "hf_cache_bucket" {
  description = "Per-region S3 bucket holding the HuggingFace model cache for this cluster"
  value       = local.bucket
}
