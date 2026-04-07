output "s3_bucket_name" {
  description = "Name of the S3 bucket for Harbor registry storage"
  value       = aws_s3_bucket.harbor_registry.bucket
}

output "s3_bucket_region" {
  description = "Region of the S3 bucket for Harbor registry storage"
  value       = aws_s3_bucket.harbor_registry.region
}

output "role_arn" {
  description = "ARN of the Harbor registry IAM role"
  value       = aws_iam_role.harbor_registry.arn
}

output "s3_access_key_id" {
  description = "Access key ID for Harbor S3 IAM user"
  value       = aws_iam_access_key.harbor_s3.id
}

output "s3_secret_access_key" {
  description = "Secret access key for Harbor S3 IAM user"
  value       = aws_iam_access_key.harbor_s3.secret
  sensitive   = true
}
