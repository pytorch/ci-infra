# Cluster Autoscaler — scales spot ASGs up (Pending pods) and down (idle nodes).
#
# Replaces the Lambda-based scale_up_spot_asg / scale_down_spot_asg_if_idle with
# the industry-standard k8s solution. Discovers ASGs via the auto-discovery tag
# k8s.io/cluster-autoscaler/<cluster-name>=owned.
#
# scale-down-unneeded-time=20m: node must be idle 20 min before removal.
# skip-nodes-with-system-pods=false: scale down even if DaemonSet pods remain.

resource "helm_release" "cluster_autoscaler" {
  # Only deploy in workspaces that have spot ASGs — prod is all on-demand/reserved.
  count            = lookup({
    "prod-east1" = 1
  }, terraform.workspace, 0)
  name             = "cluster-autoscaler"
  repository       = "https://kubernetes.github.io/autoscaler"
  chart            = "cluster-autoscaler"
  namespace        = "kube-system"
  cleanup_on_fail  = true

  values = [yamlencode({
    autoDiscovery = {
      clusterName = aws_eks_cluster.gpu_dev_cluster.name
    }
    awsRegion = local.current_config.aws_region
    extraArgs = {
      "scale-down-unneeded-time"       = "20m"
      "scale-down-delay-after-add"     = "5m"
      "scale-down-utilization-threshold" = "0.3"
      "skip-nodes-with-system-pods"    = "false"
      "skip-nodes-with-local-storage"  = "false"
      "expander"                       = "least-waste"
      "balance-similar-node-groups"    = "true"
    }
    rbac = { create = true }
    nodeSelector = { "kubernetes.io/os" = "linux" }
    tolerations = [
      { operator = "Exists" },
    ]
    resources = {
      requests = { cpu = "100m", memory = "300Mi" }
      limits   = { cpu = "200m", memory = "600Mi" }
    }
  })]

  depends_on = [aws_eks_cluster.gpu_dev_cluster]
}
