/*
 * Deploys the GitHub Actions Runner Token Service (gharts).
 *
 * gharts provides JIT runner token provisioning with OIDC-based authentication.
 * It reuses the same GitHub App credentials as ARC.
 *
 * The database is an RDS PostgreSQL instance provisioned in 01_infra.
 * Authentication uses IAM RDS auth via IRSA — no password secret is required.
 * The gharts service account is annotated with the IAM role ARN from 01_infra.
 *
 * Chart source: oci://ghcr.io/afrittoli/gharts
 */

data "aws_secretsmanager_secret_version" "gharts_arc_github_app" {
  secret_id = "pytorch-arc-github-app"
}

data "aws_secretsmanager_secret_version" "gharts_arc_github_app_private_key" {
  secret_id = "pytorch-arc-github-app-private-key"
}

locals {
  gharts_arc_config         = jsondecode(data.aws_secretsmanager_secret_version.gharts_arc_github_app.secret_string)
  gharts_github_app_id      = local.gharts_arc_config["app-id"]
  gharts_github_install_id  = local.gharts_arc_config["installation-id"]
  gharts_github_private_key = data.aws_secretsmanager_secret_version.gharts_arc_github_app_private_key.secret_string
  gharts_rds_host           = data.terraform_remote_state.runners[0].outputs.gharts_rds_host
  gharts_irsa_role_arn      = data.terraform_remote_state.runners[0].outputs.gharts_irsa_role_arn
  gharts_oidc_jwks_url      = var.gharts_oidc_jwks_url != "" ? var.gharts_oidc_jwks_url : "${var.gharts_oidc_issuer}/.well-known/jwks.json"
}

resource "kubernetes_namespace" "gharts" {
  metadata {
    name = var.gharts_namespace
  }
}

# GitHub App private key — consumed by the Helm chart as gharts-github
resource "kubernetes_secret" "gharts_github" {
  metadata {
    name      = "gharts-github"
    namespace = kubernetes_namespace.gharts.metadata[0].name
  }

  data = {
    "private-key.pem" = local.gharts_github_private_key
  }

  type = "Opaque"
}

# OIDC client ID — consumed by the Helm chart as gharts-oidc
# Populated from AWS Secrets Manager once Auth0 details are available.
resource "kubernetes_secret" "gharts_oidc" {
  metadata {
    name      = "gharts-oidc"
    namespace = kubernetes_namespace.gharts.metadata[0].name
  }

  data = {
    "oidc-client-id" = var.gharts_oidc_client_id
  }

  type = "Opaque"
}

resource "helm_release" "gharts" {
  name       = "gharts"
  repository = "oci://ghcr.io/afrittoli"
  chart      = "gharts"
  version    = var.gharts_chart_version
  namespace  = kubernetes_namespace.gharts.metadata[0].name
  wait       = true
  timeout    = 300

  values = [
    templatefile("${path.module}/values/gharts.yaml.tftpl", {
      ingress_host       = var.gharts_ingress_host
      letsencrypt_issuer = var.letsencrypt_issuer
      github_org         = var.gharts_github_org
      github_app_id      = local.gharts_github_app_id
      github_install_id  = local.gharts_github_install_id
      oidc_issuer        = var.gharts_oidc_issuer
      oidc_audience      = var.gharts_oidc_audience
      oidc_jwks_url      = local.gharts_oidc_jwks_url
      rds_host           = local.gharts_rds_host
      irsa_role_arn      = local.gharts_irsa_role_arn
      aws_region         = local.aws_region
    })
  ]

  depends_on = [
    kubernetes_secret.gharts_github,
    kubernetes_secret.gharts_oidc,
    null_resource.k8s_infra_ready,
  ]
}
