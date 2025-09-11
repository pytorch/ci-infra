resource "kubernetes_secret" "argocd_dex_github_oauth" {
  metadata {
    name      = "argocd-github-oauth"
    namespace = var.argocd_namespace
  }

  data = {
    clientSecret = var.argocd_dex_github_client_secret
  }

  type = "Opaque"
}

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
    templatefile("${path.module}/values/argocd.yaml.tftpl", {
      ingress_host              = var.argocd_ingress_host,
      letsencrypt_issuer        = var.letsencrypt_issuer,
      github_org                = var.argocd_dex_github_org
      github_team               = var.argocd_dex_github_team
      github_client_id          = var.argocd_dex_github_client_id
      github_client_secret_name = kubernetes_secret.argocd_dex_github_oauth.metadata[0].name
    })
  ]

  depends_on = [null_resource.k8s_infra_ready]
}

# Verify that the service exists with expected name and namespace
data "kubernetes_service" "argocd_server" {
  metadata {
    name      = "${helm_release.argocd.name}-server"
    namespace = helm_release.argocd.namespace
  }
  
  depends_on = [helm_release.argocd]
}
