/* 
 * This module defines an ArgoCD ApplicationSet for the RunnerScaleSets in a namespace
 *
 * Each Application in the ApplicationSet includes:
 * - A namespace, provisioned by terraform
 * - A secret with the shared GitHub App credentials, provisioned by terraform
 *   in the corresponding namespace
 * - The CR instance template from the helm chart
 * - The CR instance values, defined in an argocd folder
 */

data "aws_secretsmanager_secret_version" "arc_secrets_config" {
  secret_id = "pytorch-arc-github-app"
}

data "aws_secretsmanager_secret_version" "arc_secrets_private_key" {
  secret_id = "pytorch-arc-github-app-private-key"
}

locals {
  arc_config = jsondecode(data.aws_secretsmanager_secret_version.arc_secrets_config.secret_string)
  arc_app_id = local.arc_config["app-id"]
  arc_installation_id = local.arc_config["installation-id"]
  arc_private_key = data.aws_secretsmanager_secret_version.arc_secrets_private_key.secret_string
}

resource "kubernetes_namespace" "arc_runners" {
  metadata {
    name = var.namespace
  }
}

resource "kubernetes_secret" "github_app" {
  metadata {
    name      = "github-config"
    namespace = var.namespace
  }
  
  data = {
    github_app_id              = local.arc_app_id
    github_app_installation_id = local.arc_installation_id
    github_app_private_key     = local.arc_private_key
  }
  
  type = "Opaque"
}

/*
 * Create the ArgoCD project that contains the ApplicationSet if it does
 * not yet exists. The project is contrained to k8s namespaces with a name
 * that starts with ${var.organization}
 */
resource "argocd_project" "arc_rss_project" {
  metadata {
    name      = var.organization
    namespace = "argocd"
  }
  
  spec {
    description = "Project that includes all ${var.organization} RunnerScaleSets"
    
    source_repos = ["github.com/pytorch/ci-infra"]
    source_namespaces = ["argocd"]
    
    destination {
      namespace = "${var.organization}-*"
      server    = "*"
    }
    
    cluster_resource_blacklist {
      group = "*"
      kind  = "*"
    }

    namespace_resource_whitelist {
      group = "actions.github.com"
      kind  = "*"
    }
  }
}

/*
 * The RunnerScaleSets are provisioned by combining two sources
 * - the helm chart
 * - the value files
 *
 * Value files can be found under argocd/cloud/tenant/region/cluster/namespace/<runner-scale-set-name>.
 * Each cloud/tenant/region/cluster/namespace combination has its ApplicationSet.
 * Various RunnerSets in the same namespace belong to the same ApplicationSet, with one application each.
 *
 * The cluster folder is used to select the cluster name in ArgoCD
 * The namespace folder is used to define the target namespace in ArgoCD
 * Each resource in the folder is applied as a dedicated Application
 */
resource "argocd_application_set" "arc_runner_scale_set" {

  metadata {
    name      = "arc-rss-${var.cluster}-${var.namespace}"
    namespace = "argocd"
  }

  spec {
    generator {
      git {
        repo_url = "https://github.com/pytorch/ci-infra"
        revision = var.git_revision
        directory {
            path = "${var.provider_path}/${var.cluster}/${var.namespace}"
        }
      }
    }

    template {
      metadata {
        name      = "{{.path.basename}}"
        namespace = "argocd"
      }

      spec {
        project = argocd_project.arc_rss_project.metadata[0].name

        source {
          repo_url        = "oci://ghcr.io/actions/actions-runner-controller-charts"
          chart           = "gha-runner-scale-set"
          target_revision = "0.12.1"
          helm {
            value_files = [
              "$values/{{.path.path}}/values.yaml"
            ]
          }
        }
        source {
          repo_url        = "https://github.com/pytorch/ci-infra"
          target_revision = var.git_revision
          ref             = "values"
        }

        destination {
          server    = "{{index .path.segments 4}}"
          namespace = "{{index .path.segments 5}}"
        }
      }
    }
  }
}
