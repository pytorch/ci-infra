output "canary_monitoring_eks_cluster_name" {
  value = module.arc_canary_monitoring.cluster_name
}

output "canary_monitoring_eks_config" {
  value = {
    aws_vpc_suffix                  = module.arc_canary_monitoring.aws_vpc_suffix
    cluster_arn                     = module.arc_canary_monitoring.cluster_arn
    cluster_name                    = module.arc_canary_monitoring.cluster_name
    environment                     = module.arc_canary_monitoring.environment
    loki_access_key_id              = module.arc_canary_monitoring.loki_access_key_id
    loki_secret_access_key          = module.arc_canary_monitoring.loki_secret_access_key
    loki_admin_bucket               = module.arc_canary_monitoring.loki_admin_bucket
    loki_chunks_bucket              = module.arc_canary_monitoring.loki_chunks_bucket
    loki_ruler_bucket               = module.arc_canary_monitoring.loki_ruler_bucket
    security_group_ids              = module.arc_canary_monitoring.security_group_ids
    subnet_ids                      = module.arc_canary_monitoring.subnet_ids
  }
  sensitive = true
}

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

output "prod_monitoring_eks_cluster_name" {
  value = module.arc_prod_monitoring.cluster_name
}

output "prod_monitoring_eks_config" {
  value = {
    cluster_arn        = module.arc_prod_monitoring.cluster_arn
    cluster_name       = module.arc_prod_monitoring.cluster_name
    security_group_ids = module.arc_prod_monitoring.security_group_ids
    subnet_ids         = module.arc_prod_monitoring.subnet_ids
  }
}
