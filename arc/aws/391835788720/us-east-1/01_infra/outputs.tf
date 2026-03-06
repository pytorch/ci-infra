# Essential outputs for Helm provider
output "cluster_endpoint" {
  description = "EKS cluster endpoint URL"
  value       = module.pytorch_arc_dev_eks.cluster_endpoint
}

output "cluster_ca_certificate" {
  description = "Base64 encoded certificate data required to communicate with the cluster"
  value       = module.pytorch_arc_dev_eks.cluster_certificate_authority_data
  sensitive   = true
}

output "cluster_name" {
  description = "EKS cluster name"
  value       = module.pytorch_arc_dev_eks.cluster_name
}

output "cluster_id" {
  description = "EKS cluster ID"
  value         = module.pytorch_arc_dev_eks.cluster_id
}

output "oidc_provider_arn" {
  description = "ARN of the cluster OIDC provider"
  value         = module.pytorch_arc_dev_eks.oidc_provider_arn
}

output "cluster_oidc_issuer_url" {
  description = "URL of the OIDC Issuer"
  value         = module.pytorch_arc_dev_eks.cluster_oidc_issuer_url
}

output "vpc_id" {
  description = "The ID of the cluster vpc"
  value       = module.arc_runners_vpc.vpc_id
}
