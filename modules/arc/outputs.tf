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

output "docker_registry_bucket" {
  value = aws_s3_bucket.internal_docker_registry.bucket
}

output "docker_registry_bucket_arn" {
  value = aws_s3_bucket.internal_docker_registry.arn
}

output "docker_registry_user" {
  value = aws_iam_user.internal_docker_registry_usr.name
}

output "docker_registry_user_arn" {
  value = aws_iam_user.internal_docker_registry_usr.arn
}

output "docker_registry_user_access_key" {
  value = aws_iam_access_key.internal_docker_registry_usr_key.id
}

output "docker_registry_user_secret" {
  value = aws_iam_access_key.internal_docker_registry_usr_key.secret
}
