output "canary_eks_cluster_name" {
  value = [for mod in module.arc_canary: mod.cluster_name]
}

output "canary_eks_config" {
  value = {
    for mod in module.arc_canary:
    mod.cluster_name => {
      cluster_name                  = mod.cluster_name
      cluster_arn                   = mod.cluster_arn
      karpenter_node_role_name      = mod.karpenter_node_role_name
      karpenter_node_role_arn       = mod.karpenter_node_role_arn
      karpenter_controler_role_name = mod.karpenter_controler_role_name
      karpenter_controler_role_arn  = mod.karpenter_controler_role_arn
      subnet_ids                    = mod.subnet_ids
      security_group_ids            = mod.security_group_ids
    }
  }
}

output "vanguard_eks_cluster_name" {
  value = [for mod in module.arc_vanguard: mod.cluster_name]
}

output "vanguard_eks_config" {
  value = {
    for mod in module.arc_vanguard:
    mod.cluster_name => {
      cluster_name                  = mod.cluster_name
      cluster_arn                   = mod.cluster_arn
      karpenter_node_role_name      = mod.karpenter_node_role_name
      karpenter_node_role_arn       = mod.karpenter_node_role_arn
      karpenter_controler_role_name = mod.karpenter_controler_role_name
      karpenter_controler_role_arn  = mod.karpenter_controler_role_arn
      subnet_ids                    = mod.subnet_ids
      security_group_ids            = mod.security_group_ids
    }
  }
}

output "prod_eks_cluster_name" {
  value = [for mod in module.arc_prod: mod.cluster_name]
}

output "prod_eks_config" {
  value = {
    for mod in module.arc_prod:
    mod.cluster_name => {
      cluster_name                  = mod.cluster_name
      cluster_arn                   = mod.cluster_arn
      karpenter_node_role_name      = mod.karpenter_node_role_name
      karpenter_node_role_arn       = mod.karpenter_node_role_arn
      karpenter_controler_role_name = mod.karpenter_controler_role_name
      karpenter_controler_role_arn  = mod.karpenter_controler_role_arn
      subnet_ids                    = mod.subnet_ids
      security_group_ids            = mod.security_group_ids
    }
  }
}
