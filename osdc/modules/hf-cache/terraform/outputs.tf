output "role_arn" {
  description = "IRSA role ARN for the hf-cache-mount service account (read-write)"
  value       = aws_iam_role.hf_cache.arn
}

output "hf_cache_bucket" {
  description = "This cluster's HuggingFace model-cache S3 bucket"
  value       = local.bucket
}
