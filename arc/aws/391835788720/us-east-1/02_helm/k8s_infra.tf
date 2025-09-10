# This file includes basic k8s infrastructure managed via helm
# Other k8s services must depend on resources defined here

resource "helm_release" "ingress_nginx" {
  name             = "ingress-nginx"
  repository       = "https://kubernetes.github.io/ingress-nginx"
  chart            = "ingress-nginx"
  namespace        = "ingress-nginx"
  version          = "4.13.1"
  create_namespace = true

  # Load values from dedicated file
  values = [
    file("${path.module}/values/ingress-nginx.yaml")
  ]
}

resource "helm_release" "cert_manager" {
  name             = "cert-manager"
  repository       = "https://charts.jetstack.io"
  chart            = "cert-manager"
  namespace        = "cert-manager"
  version          = "v1.13.2"
  create_namespace = true

  values = [
    file("${path.module}/values/cert-manager.yaml")
  ]
}

resource "kubernetes_manifest" "letsencrypt_prod" {
  manifest = yamldecode(templatefile("${path.module}/resources/clusterissuer.yaml.tftpl", {
    cert_manager_email  = var.cert_manager_email
    letsencrypt_issuer = var.letsencrypt_issuer
  }))

  depends_on = [helm_release.cert_manager]
}


# This gives a single depends_on target for all other apps installed on k8s
resource "null_resource" "k8s_infra_ready" {
  depends_on = [
    helm_release.ingress_nginx,
    helm_release.cert_manager,
    kubernetes_manifest.letsencrypt_prod,
  ]
}
