output "cluster_name" {
  description = "EKS cluster name"
  value       = aws_eks_cluster.this.name
}

output "cluster_id" {
  description = "EKS cluster ID"
  value       = aws_eks_cluster.this.id
}

output "cluster_arn" {
  description = "EKS cluster ARN"
  value       = aws_eks_cluster.this.arn
}

output "cluster_endpoint" {
  description = "Endpoint for EKS control plane"
  value       = aws_eks_cluster.this.endpoint
}

output "cluster_security_group_id" {
  description = "Security group ID attached to the EKS cluster"
  value       = aws_eks_cluster.this.vpc_config[0].cluster_security_group_id
}

output "oidc_provider_arn" {
  description = "ARN of the OIDC provider for IRSA"
  value       = var.enable_irsa ? aws_iam_openid_connect_provider.cluster[0].arn : null
}

output "oidc_provider" {
  description = "OIDC provider URL without https://"
  value       = var.enable_irsa ? replace(aws_eks_cluster.this.identity[0].oidc[0].issuer, "https://", "") : null
}

output "node_instance_role_arn" {
  description = "IAM role ARN for worker nodes"
  value       = aws_iam_role.node.arn
}

output "node_instance_role_name" {
  description = "IAM role name for worker nodes"
  value       = aws_iam_role.node.name
}

output "cluster_certificate_authority_data" {
  description = "Base64 encoded certificate data required to communicate with the cluster"
  value       = aws_eks_cluster.this.certificate_authority[0].data
  sensitive   = true
}

output "cluster_version" {
  description = "The Kubernetes version for the cluster"
  value       = aws_eks_cluster.this.version
}

output "eks_secrets_kms_key_arn" {
  description = "ARN of the KMS key used for EKS secrets encryption"
  value       = var.enable_secrets_encryption ? aws_kms_key.eks_secrets[0].arn : null
}

output "eks_secrets_kms_key_id" {
  description = "ID of the KMS key used for EKS secrets encryption"
  value       = var.enable_secrets_encryption ? aws_kms_key.eks_secrets[0].key_id : null
}
