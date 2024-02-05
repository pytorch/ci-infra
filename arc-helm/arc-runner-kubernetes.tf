resource "helm_release" "arc_runner_kubernetes" {
  depends_on = [helm_release.arc]

  name       = "arc-runner-kubernetes"
  namespace = kubernetes_namespace.arc_runners.metadata.0.name

  repository = "oci://ghcr.io/actions/actions-runner-controller-charts/"
  chart      = "gha-runner-scale-set"

  values = [
    "${file("arc-runner-kubernetes-values.yaml")}"
  ]
}
