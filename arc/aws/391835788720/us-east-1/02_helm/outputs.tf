# Essential outputs for Helm provider
output "cluster_endpoint" {
  description = "EKS cluster endpoint URL"
  value       = module.pytorch_arc_dev_eks.cluster_endpoint
}

output "cluster_ca_certificate" {
  description = "Base64 encoded certificate data required to communicate with the cluster"
  value       = module.pytorch_arc_dev_eks.cluster_certificate_authority_data
}

output "cluster_name" {
  description = "EKS cluster name"
  value       = module.pytorch_arc_dev_eks.cluster_name
}

output "cluster_id" {
  description = "EKS cluster ID"
  value         = module.pytorch_arc_dev_eks.cluster_id
}
