/*
 * Installs ARC control plane. This reconciles RunnerScaleSets once they are defined
 * Since GitHub credentials are part of the RunnerScaleSet CRD, this controller does
 * not connect to GitHub until any RunnerScaleSet exists.
 *
 * ARC provides and helm chart for the RunnerScaleSets
 */

resource "helm_release" "arc" {
  name       = "arc"
  repository = "oci://ghcr.io/actions/actions-runner-controller-charts"
  chart      = "gha-runner-scale-set-controller"
  namespace  = "arc-system"
  version    = "0.12.1"

  create_namespace = true

  values = [
    file("${path.module}/values/gha-runner-scale-set-controller.yaml")
  ]

  depends_on = [
    null_resource.k8s_infra_ready,
  ]
}
