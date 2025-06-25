output "cluster_name" {
  value = module.eks.cluster_name
}

output "cluster_arn" {
  value = module.eks.cluster_arn
}

output "subnet_ids" {
  value = var.subnet_ids
}

output "security_group_ids" {
  value = [module.eks.node_security_group_id]
}

output "environment" {
  value = var.environment
}

output "aws_vpc_suffix" {
  value = var.aws_vpc_suffix
}

output "loki_chunks_bucket" {
  value = aws_s3_bucket.loki_chunks.id
}

output "loki_ruler_bucket" {
  value = aws_s3_bucket.loki_ruler.id
}

output "loki_admin_bucket" {
  value = aws_s3_bucket.loki_admin.id
}

output "loki_access_key_id" {
  value = aws_iam_access_key.loki.id
}

output "loki_secret_access_key" {
  value = aws_iam_access_key.loki.secret
}
