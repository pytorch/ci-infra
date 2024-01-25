output "cluster_name" {
  value = local.cluster_name
}

output "cluster_arn" {
  value = module.eks.cluster_arn
}

output "karpenter_node_role_name" {
  value = aws_iam_role.karpenter_node_role.name
}

output "karpenter_node_role_arn" {
  value = aws_iam_role.karpenter_node_role.arn
}

output "karpenter_controler_role_name" {
  value = aws_iam_role.karpenter_controler_role.name
}

output "karpenter_controler_role_arn" {
  value = aws_iam_role.karpenter_controler_role.arn
}

output "subnet_ids" {
  value = var.subnet_ids
}

output "security_group_ids" {
  value = [module.eks.node_security_group_id]
}
