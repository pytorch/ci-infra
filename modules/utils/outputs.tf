output "canary_eks_cluster_name" {
  value = [for mod in module.arc_canary: mod.cluster_name]
}

output "canary_eks_config" {
  value = {
    for mod in module.arc_canary:
    mod.cluster_name => {
      cluster_arn                     = mod.cluster_arn
      cluster_name                    = mod.cluster_name
      docker_registry_bucket          = mod.docker_registry_bucket
      docker_registry_bucket_arn      = mod.docker_registry_bucket_arn
      docker_registry_user            = mod.docker_registry_user
      docker_registry_user_access_key = mod.docker_registry_user_access_key
      docker_registry_user_arn        = mod.docker_registry_user_arn
      docker_registry_user_secret     = mod.docker_registry_user_secret
      internal_registry_secret_arn    = mod.internal_registry_secret_arn
      karpenter_controler_role_arn    = mod.karpenter_controler_role_arn
      karpenter_controler_role_name   = mod.karpenter_controler_role_name
      karpenter_node_role_arn         = mod.karpenter_node_role_arn
      karpenter_node_role_name        = mod.karpenter_node_role_name
      security_group_ids              = mod.security_group_ids
      subnet_ids                      = mod.subnet_ids
    }
  }
  sensitive = true
}

output "vanguard_eks_cluster_name" {
  value = [for mod in module.arc_vanguard: mod.cluster_name]
}

output "vanguard_eks_config" {
  value = {
    for mod in module.arc_vanguard:
    mod.cluster_name => {
      cluster_arn                     = mod.cluster_arn
      cluster_name                    = mod.cluster_name
      docker_registry_bucket          = mod.docker_registry_bucket
      docker_registry_bucket_arn      = mod.docker_registry_bucket_arn
      docker_registry_user            = mod.docker_registry_user
      docker_registry_user_access_key = mod.docker_registry_user_access_key
      docker_registry_user_arn        = mod.docker_registry_user_arn
      docker_registry_user_secret     = mod.docker_registry_user_secret
      internal_registry_secret_arn    = mod.internal_registry_secret_arn
      karpenter_controler_role_arn    = mod.karpenter_controler_role_arn
      karpenter_controler_role_name   = mod.karpenter_controler_role_name
      karpenter_node_role_arn         = mod.karpenter_node_role_arn
      karpenter_node_role_name        = mod.karpenter_node_role_name
      security_group_ids              = mod.security_group_ids
      subnet_ids                      = mod.subnet_ids    }
  }
  sensitive = true
}

output "prod_eks_cluster_name" {
  value = [for mod in module.arc_prod: mod.cluster_name]
}

output "prod_eks_config" {
  value = {
    for mod in module.arc_prod:
    mod.cluster_name => {
      cluster_arn                     = mod.cluster_arn
      cluster_name                    = mod.cluster_name
      docker_registry_bucket          = mod.docker_registry_bucket
      docker_registry_bucket_arn      = mod.docker_registry_bucket_arn
      docker_registry_user            = mod.docker_registry_user
      docker_registry_user_access_key = mod.docker_registry_user_access_key
      docker_registry_user_arn        = mod.docker_registry_user_arn
      docker_registry_user_secret     = mod.docker_registry_user_secret
      internal_registry_secret_arn    = mod.internal_registry_secret_arn
      karpenter_controler_role_arn    = mod.karpenter_controler_role_arn
      karpenter_controler_role_name   = mod.karpenter_controler_role_name
      karpenter_node_role_arn         = mod.karpenter_node_role_arn
      karpenter_node_role_name        = mod.karpenter_node_role_name
      security_group_ids              = mod.security_group_ids
      subnet_ids                      = mod.subnet_ids    }
  }
  sensitive = true
}

output "prometheus_production_endpoint" {
  value = aws_prometheus_workspace.monitoring_production.prometheus_endpoint
}

output "prometheus_canary_endpoint" {
  value = aws_prometheus_workspace.monitoring_canary.prometheus_endpoint
}

output "grafana_production_endpoint" {
  value = aws_grafana_workspace.monitoring_production.endpoint
}

output "grafana_canary_endpoint" {
  value = aws_grafana_workspace.monitoring_canary.endpoint
}
