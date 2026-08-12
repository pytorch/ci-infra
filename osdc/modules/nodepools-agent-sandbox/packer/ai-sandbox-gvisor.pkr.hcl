# Custom EKS AL2023 AMI with gVisor (runsc) baked in, for the ai-sandbox fleet.
#
# Build:  just build-agent-sandbox-ami <cluster>
#
# Why an AMI instead of a userData script: on a Karpenter-managed fleet the node
# must be ready the moment it registers. Installing runsc at boot meant fetching
# binaries over the network on every scale-up and then RESTARTING containerd
# after kubelet had already started — a restart that races anything already
# scheduled on the node. Baking it means containerd starts once, correct.
#
# The runsc runtime handler is registered by dropping a nodeadm NodeConfig at
# /etc/eks/nodeadm.d/. nodeadm merges configs from that directory with the
# user-data config and generates /etc/containerd/config.toml *before* starting
# containerd, so the handler exists at first start. Writing config.toml directly
# in the image would not work — nodeadm regenerates it at every boot.
#
# The source AMI is resolved from the EKS-optimized AL2023 SSM parameter at build
# time, so each rebuild picks up the current patched base. That freshness is now
# a rebuild cadence rather than automatic: the stock fleets track
# `alias: al2023@latest` and pick up CVE fixes on node rotation, while this fleet
# only moves when the AMI is rebuilt. See the module README.

packer {
  required_version = ">= 1.10"

  required_plugins {
    amazon = {
      version = ">= 1.3"
      source  = "github.com/hashicorp/amazon"
    }
  }
}

variable "aws_region" {
  description = "Region to build the AMI in (must be the cluster's region — AMIs are regional)"
  type        = string
}

variable "cluster_name" {
  description = "Cluster this AMI is built for; recorded in tags"
  type        = string
}

variable "k8s_version" {
  description = "Kubernetes minor version of the EKS-optimized base AMI (must match the cluster)"
  type        = string
}

variable "gvisor_release" {
  description = "gVisor release date to pin (yyyymmdd), from https://gvisor.dev/docs/user_guide/install/"
  type        = string
  # Pinned deliberately: the previous userData script fetched `latest`, so two
  # nodes launched a day apart could run different runsc builds. Bump this
  # explicitly and rebuild.
  default = "20260803"
}

variable "subnet_filter_name" {
  description = "Name tag pattern for the build subnet; must have outbound internet (fetches the gVisor release)"
  type        = string
  default     = "*-vpc-public-*"
}

variable "build_instance_type" {
  description = "Instance type used only for the build"
  type        = string
  default     = "c7i.xlarge"
}

locals {
  ami_name = "osdc-ai-sandbox-gvisor-k8s${var.k8s_version}-${var.gvisor_release}-${formatdate("YYYYMMDDhhmmss", timestamp())}"
}

# Latest EKS-optimized AL2023 AMI for this Kubernetes version.
data "amazon-parameterstore" "eks_al2023" {
  name   = "/aws/service/eks/optimized-ami/${var.k8s_version}/amazon-linux-2023/x86_64/standard/recommended/image_id"
  region = var.aws_region
}

source "amazon-ebs" "ai_sandbox" {
  region        = var.aws_region
  source_ami    = data.amazon-parameterstore.eks_al2023.value
  instance_type = var.build_instance_type
  ssh_username  = "ec2-user"
  ami_name      = local.ami_name
  ami_description = "EKS AL2023 + gVisor ${var.gvisor_release} for the OSDC ai-sandbox fleet"

  # IMDSv2. Two separate reasons, both required:
  #   * metadata_options applies to the BUILD instance. An org SCP explicitly
  #     denies ec2:RunInstances unless ec2:MetadataHttpTokens=required, so a
  #     build without this fails with UnauthorizedOperation.
  #   * imds_support makes the resulting AMI default to IMDSv2-only. The sandbox
  #     threat model leans on IMDS being unreachable from pods (hop limit 1), and
  #     IMDSv1 would let a compromised agent bypass that with a plain GET.
  imds_support = "v2.0"
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  # Build in the cluster VPC's public subnet — the account has no reliable
  # default VPC, and the build needs egress to fetch the gVisor release.
  subnet_filter {
    filters = {
      "tag:Name" = "${var.cluster_name}${var.subnet_filter_name}"
    }
    most_free = true
    random    = false
  }
  associate_public_ip_address = true

  # Karpenter selects this AMI by tag (see defs/ai-sandbox.yaml) rather than by
  # name glob, so the naming scheme can change without touching the nodepool.
  tags = {
    Name           = local.ami_name
    "osdc.io/ami"  = "ai-sandbox-gvisor"
    "osdc.io/module" = "nodepools-agent-sandbox"
    Cluster        = var.cluster_name
    GvisorRelease  = var.gvisor_release
    K8sVersion     = var.k8s_version
    SourceAMI      = data.amazon-parameterstore.eks_al2023.value
    Project        = "ciforge"
  }
  snapshot_tags = {
    Name          = local.ami_name
    "osdc.io/ami" = "ai-sandbox-gvisor"
  }
}

build {
  name    = "ai-sandbox-gvisor"
  sources = ["source.amazon-ebs.ai_sandbox"]

  provisioner "shell" {
    script           = "${path.root}/scripts/install-gvisor.sh"
    environment_vars = ["GVISOR_RELEASE=${var.gvisor_release}"]
    # {{ .Vars }} is required: overriding execute_command drops the default
    # environment_vars injection, and the script hard-fails without GVISOR_RELEASE.
    execute_command = "{{ .Vars }} sudo -E bash -eux '{{ .Path }}'"
  }

  post-processor "manifest" {
    output     = "${path.root}/manifest.json"
    strip_path = true
  }
}
