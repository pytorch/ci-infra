resource "helm_release" "arc" {
  name       = "arc"
  namespace = kubernetes_namespace.arc_systems.metadata.0.name

  repository = "oci://ghcr.io/actions/actions-runner-controller-charts/"
  chart      = "gha-runner-scale-set-controller"
}
