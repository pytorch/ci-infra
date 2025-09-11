# Outputs for ArgoCD provider

output "argocd_release_info" {
  description = "ArgoCD Release Details"
  value = {
    name      = helm_release.argocd.name
    namespace = helm_release.argocd.namespace
    status    = helm_release.argocd.status
    chart     = helm_release.argocd.chart
    version   = helm_release.argocd.version
  }
}

# Used by the following layer to initialise the k8s provider
# Ensure that the argocd layer only depends on the helm one
output "cluster_info" {
  description = "Details of the underlying Kubernetes cluster"
  value = {
    endpoint      = data.terraform_remote_state.runners[0].outputs.cluster_endpoint
    ca_certificate = base64decode(data.terraform_remote_state.runners[0].outputs.cluster_ca_certificate)
    name          = data.terraform_remote_state.runners[0].outputs.cluster_name
  }
}

# ArgoCD is available through a public endpoint, but we use the internal endpoint
# for the ArgoCD terraform provider. The public endpoint depends on the cert-manager
# ClusterIssuer that is only provisioned in the next layer (see comments there), which
# means that terraform cannot assume that the public endpoint is up and running at the
# time when ArgoCD applications are deployed.
output "argocd_endpoint" {
  description = "ArgoCD Endpoint"
  value = "http://${data.kubernetes_service.argocd_server.metadata[0].name}.${helm_release.argocd.namespace}.svc.cluster.local"
}

output "argocd_admin_secret_name" {
  description = "The name of the secret that holds the admin credentials"
  value = "argocd-initial-admin-secret"
}
