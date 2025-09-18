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

# ClusterRole with secret read permissions
# This is never used cluster-wide, only in specific namespaces
resource "kubernetes_cluster_role" "secret_reader" {
  metadata {
    name = "secret-reader"
  }

  rule {
    api_groups = [""]
    resources  = ["secrets"]
    verbs      = ["get", "list", "watch"]
  }
}

/*
 * A few resources need to be provisioned for each RunnerScaleSet
 * i.e. for each folder under ${var.provider_path}/${var.cluster}
 * 
 * - A namespace
 * - The GitHub secret
 * - A RoleBinding the allows access to the secret 
 */

// Find folders directly under argocd/${var.provider_path}/${var.cluster}
// and create a JSON map of results for next resources to loop on
data "external" "runner_scale_sets" {
  program = ["bash", "-c", <<-EOT
    REPO_ROOT=$(git rev-parse --show-toplevel)
    find "$REPO_ROOT/${var.provider_path}/${var.cluster}" -mindepth 1 -maxdepth 1 -type d -type d -exec basename {} \; | jq -R -s -c 'split("\n")[:-1] | map({key: ., value: .}) | from_entries'
  EOT
  ]
}

resource "kubernetes_namespace" "arc_runners" {
  for_each = data.external.runner_scale_sets.result

  metadata {
    name = "${var.organization}-${each.value}"
  }
}

resource "kubernetes_secret" "github_app" {
  for_each = data.external.runner_scale_sets.result

  metadata {
    name      = "github-config"
    namespace = "${var.organization}-${each.value}"
  }
  
  data = {
    github_app_id              = local.arc_app_id
    github_app_installation_id = local.arc_installation_id
    github_app_private_key     = local.arc_private_key
  }
  
  type = "Opaque"
}

resource "kubernetes_role_binding" "secret_access" {
  for_each = data.external.runner_scale_sets.result

  metadata {
    name      = "secret-reader-binding"
    namespace = "${var.organization}-${each.value}"
  }

  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "ClusterRole"
    name      = kubernetes_cluster_role.secret_reader.metadata[0].name
  }

  subject {
    kind      = "ServiceAccount"
    name      = var.arc_controller_sa
    namespace = var.arc_controller_sa_namespace
  }
}

// This secret is required by ArgoCD to enable using an OCI repo
// for helm charts. It's deployed once in the argocd namespace.
resource "kubernetes_secret" "arc_runner_scale_set_oci_repo" {
  metadata {
    name      = "arc-runner-scale-set-oci-repo"
    namespace = "argocd"
    labels    = {
      "argocd.argoproj.io/secret-type" = "repository"
    }
  }
  
  data = {
    url       = "ghcr.io/actions/actions-runner-controller-charts"
    name      = "actions-runner-controller"
    type      = "helm"
    enableOCI = "true"
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
    
    source_repos = [
      "https://github.com/pytorch/ci-infra",
      "ghcr.io/actions/actions-runner-controller-charts",
    ]
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

    namespace_resource_whitelist {
      group = ""
      kind  = "ServiceAccount"
    }

    namespace_resource_whitelist {
      group = "rbac.authorization.k8s.io"
      kind  = "Role"
    }

    namespace_resource_whitelist {
      group = "rbac.authorization.k8s.io"
      kind  = "RoleBinding"
    }
  }
}

/*
 * The RunnerScaleSets are provisioned by combining two sources
 * - the helm chart
 * - the value files
 *
 * Value files can be found under argocd/cloud/tenant/region/cluster/<runner-scale-set-name>.
 * Each cloud/tenant/region/cluster combination has its ApplicationSet.
 * Each RunnerSets runs in its own namespace belong to the ApplicationSet, with one application each.
 *
 * The cluster folder is used to select the cluster name in ArgoCD
 * Each resource in the folder is applied as a dedicated Application
 */
resource "argocd_application_set" "arc_runner_scale_set" {

  metadata {
    name      = "arc-rss-${var.organization}-${var.cluster}"
    namespace = "argocd"
  }

  spec {
    go_template = true
    go_template_options = ["missingkey=error"]

    generator {
      git {
        repo_url = "https://github.com/pytorch/ci-infra"
        revision = var.git_revision
        directory {
            path = "${var.provider_path}/${var.cluster}/*"
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
          repo_url        = "ghcr.io/actions/actions-runner-controller-charts"
          path            = "gha-runner-scale-set"
          chart           = "gha-runner-scale-set"
          target_revision = "0.12.1"
          helm {
            value_files = [
              "$values/{{.path.path}}/values.yaml"
            ]
            values = <<-EOT
              controllerServiceAccount:
                name: ${var.arc_controller_sa}
                namespace: ${var.arc_controller_sa_namespace}
            EOT
          }
        }
        source {
          repo_url        = "https://github.com/pytorch/ci-infra"
          target_revision = var.git_revision
          ref             = "values"
        }

        destination {
          name      = "{{index .path.segments 4}}"
          namespace = "${var.organization}-{{index .path.segments 5}}"
        }

        sync_policy {
          automated {
            prune    = true
            self_heal = true
          }
        }
      }
    }
  }
}
