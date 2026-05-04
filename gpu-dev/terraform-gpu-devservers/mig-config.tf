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

# Optional declarative B200 MIG node label. Set b200_mig_node_name in tfvars (or override the
# variable's default below) to dedicate a specific B200 node to the mixed profile. Empty string
# means "no node currently labelled" — the existing all-disabled stays in effect.
variable "b200_mig_node_name" {
  description = "Hostname of the B200 node to label with nvidia.com/mig.config=b200-6full-2mig-balanced. Leave empty to skip."
  type        = string
  default     = ""
}

resource "kubernetes_labels" "b200_mig_node" {
  count = var.b200_mig_node_name == "" ? 0 : 1

  api_version = "v1"
  kind        = "Node"

  metadata {
    name = var.b200_mig_node_name
  }

  labels = {
    "nvidia.com/mig.config" = "b200-6full-2mig-balanced"
  }

  # Take ownership of the label even if another tool (kubectl, gpu-operator) set it.
  force = true

  depends_on = [kubernetes_config_map.gpu_dev_mig_parted_config]
}
