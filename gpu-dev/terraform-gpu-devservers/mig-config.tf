# mig-config.tf — fork the NVIDIA mig-parted-config ConfigMap so we can add custom profiles
# without fighting NVIDIA ClusterPolicy's reconciliation of its default-mig-parted-config.
#
# The vendored mig-parted-config.yaml in this directory mirrors the upstream profiles plus our
# additions (e.g. b200-6full-2mig-balanced). Helm is told to use this ConfigMap by name via
# migManager.config.name in kubernetes.tf, so the GPU operator skips creating its default and
# reads ours instead.

resource "kubernetes_config_map" "gpu_dev_mig_parted_config" {
  metadata {
    name      = "gpu-dev-mig-parted-config"
    namespace = "gpu-operator"
    labels = {
      "app.kubernetes.io/managed-by" = "terraform"
      "app.kubernetes.io/part-of"    = "gpu-dev-servers"
    }
  }

  data = {
    "config.yaml" = file("${path.module}/mig-parted-config.yaml")
  }

  # The gpu-operator namespace is created by the helm release; depend on that so this ConfigMap
  # lands AFTER the namespace exists.
  depends_on = [helm_release.nvidia_gpu_operator]
}

# Declarative B200 MIG node label. Set b200_mig_node_name (per workspace via the locals lookup
# below, or override via tfvars / -var) to dedicate a specific B200 node to the mixed profile.
# Empty string means "no node labelled" — every B200 stays full.
#
# Future cleanup: when we split a B200 CR into two ASGs (one with mig_profile, one without),
# the user_data path will set this label at boot for any instance in the MIG-dedicated ASG —
# matching the H100 cr3 pattern. Until then, this declarative label pins the role to a hostname.
locals {
  # Workspace-scoped defaults so the resource is a no-op in non-prod and no apply ever tries to
  # label a node that doesn't exist.
  default_b200_mig_node_by_workspace = {
    # B200 MIG now handled by cr4 ASG with auto mig_profile label
    prod = ""
  }
  b200_mig_node_effective = (
    var.b200_mig_node_name != ""
    ? var.b200_mig_node_name
    : lookup(local.default_b200_mig_node_by_workspace, terraform.workspace, "")
  )
}

variable "b200_mig_node_name" {
  description = "Hostname of the B200 node to label with nvidia.com/mig.config=b200-6full-2mig-balanced. Leave empty to use the per-workspace default in mig-config.tf."
  type        = string
  default     = ""
}

resource "kubernetes_labels" "b200_mig_node" {
  count = local.b200_mig_node_effective == "" ? 0 : 1

  api_version = "v1"
  kind        = "Node"

  metadata {
    name = local.b200_mig_node_effective
  }

  labels = {
    "nvidia.com/mig.config" = "b200-6full-2mig-balanced"
  }

  # Take ownership of the label even if another tool (kubectl, gpu-operator) set it.
  force = true

  depends_on = [kubernetes_config_map.gpu_dev_mig_parted_config]
}
