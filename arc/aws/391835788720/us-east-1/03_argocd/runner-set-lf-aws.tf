# Runners sets in the lf-aws namespace

module "argocd-runner-scale-set" {
  source      = "../../../../modules/argocd-runner-scale-set/"

  server_addr   = local.argocd_ready ? local.argocd_endpoint : null
  token         = local.argocd_ready ? local.argocd_terraform_sa_token : null
  organization  = "lf"
  cluster       = "in-cluster"
  namespace     = "lf-aws"
  provider_path = "argocd/aws/391835788720/us-east-1"
  git_revision  = var.git_revision
}
