output "cluster_name" {
  value = local.cluster_name
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
