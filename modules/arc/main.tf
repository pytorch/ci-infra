terraform {
  required_version = ">= 1.5"
  required_providers {
    random = {
      source  = "hashicorp/random"
      version = ">= 3.4"
    }
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.5"
    }
    helm = {
      source = "hashicorp/helm"
      version = ">= 2.12"
    }
  }
}

resource "kubernetes_namespace" "arc_systems" {
  metadata {
    name = "arc-systems"
  }
}

resource "kubernetes_namespace" "arc_runners" {
  metadata {
    name = "arc-runners"
  }
}

resource "helm_release" "arc_controller" {
  name       = "arc-controller"
  namespace = kubernetes_namespace.arc_systems.metadata.0.name

  repository = "oci://ghcr.io/actions/actions-runner-controller-charts/"
  chart      = "gha-runner-scale-set-controller"

  values = [
    "${templatefile("${path.module}/arc-config/arc-controller-values.tftpl", {})}"
  ]
}

resource "helm_release" "arc_runner_dind" {
  depends_on = [helm_release.arc_controller]

  name       = "arc-runner-dind"
  namespace = kubernetes_namespace.arc_runners.metadata.0.name

  repository = "oci://ghcr.io/actions/actions-runner-controller-charts/"
  chart      = "gha-runner-scale-set"

  values = [
    "${templatefile("${path.module}/arc-config/arc-runner-dind-values.tftpl", {
        github_config_url = "https://github.com/pytorch/test-infra",
        github_app_id = "${var.github_app_id}",
        github_app_installation_id = "${var.github_app_installation_id}",
        github_app_private_key = "${var.github_app_private_key}"
    })}"
  ]
}
