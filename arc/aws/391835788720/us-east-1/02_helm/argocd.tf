resource "helm_release" "argocd" {
  name             = "argocd"
  repository       = "https://argoproj.github.io/argo-helm"
  chart            = "argo-cd"
  version          = var.argocd_version
  namespace        = var.argocd_namespace
  create_namespace = true
  wait             = true
  timeout          = 600

  values = [
    # Nothing to pass to the template for now
    templatefile("${path.module}/values/argocd.yaml.tftpl", {})
  ]
}

# Verify that the service exists with expected name and namespace
data "kubernetes_service" "argocd_server" {
  metadata {
    name      = "${helm_release.argocd.name}-server"
    namespace = helm_release.argocd.namespace
  }
  
  depends_on = [helm_release.argocd]
}
