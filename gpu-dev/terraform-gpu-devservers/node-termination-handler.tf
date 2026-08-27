# AWS Node Termination Handler — graceful drain on spot-interrupt + ASG lifecycle events.
#
# IMDS mode (one DaemonSet per node, no SQS / no IAM role) is plenty for our use case:
# we don't care about queue-processor features (rebalance recommendations, scheduled
# events). We just want pods to get a clean SIGTERM when AWS sends the 2-minute spot
# notice via instance metadata, instead of being killed cold.
#
# Tolerates everything so it runs on the GPU nodes that have nvidia.com/gpu:NoSchedule.

resource "helm_release" "aws_node_termination_handler" {
  name             = "aws-node-termination-handler"
  repository       = "https://aws.github.io/eks-charts"
  chart            = "aws-node-termination-handler"
  namespace        = "kube-system"
  # No version pin — chart versions advance frequently and my first guess (0.27.1)
  # didn't exist. helm picks current latest stable. Add a pin once we hit a regression.
  cleanup_on_fail  = true

  values = [yamlencode({
    enableSpotInterruptionDraining = true
    enableScheduledEventDraining   = true
    enableRebalanceMonitoring      = true
    enableRebalanceDraining        = false # warning only; rebalance recommendations are too noisy
    nodeSelector = {
      "kubernetes.io/os" = "linux"
    }
    tolerations = [
      { operator = "Exists" }, # tolerate every taint; we want NTH on every node, including GPU nodes
    ]
    resources = {
      requests = { cpu = "50m", memory = "64Mi" }
      limits   = { cpu = "100m", memory = "128Mi" }
    }
  })]

  depends_on = [aws_eks_cluster.gpu_dev_cluster]
}
