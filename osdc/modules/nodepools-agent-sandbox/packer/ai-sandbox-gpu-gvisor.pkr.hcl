# EKS AL2023 NVIDIA AMI with gVisor (runsc) + nvproxy baked in, for the ai-sandbox-gpu
# fleet.
#
#   just build-agent-sandbox-gpu-ami <cluster>
#
# Same shape as ai-sandbox-gvisor.pkr.hcl, with two differences that matter:
#
#   1. The base is the NVIDIA variant of the EKS AMI, pinned to an exact release rather
#      than `recommended`. nvproxy only supports an explicit list of driver versions, and
#      `recommended` currently ships 580.178.04, which is NOT on it. See
#      docs/agent-sandbox-gpu-gvisor.md for the pinning policy — driver and runsc move
#      together, and neither moves alone.
#   2. nvproxy is enabled, which forwards NVIDIA ioctls from the sandbox to the host
#      driver. That is the point of the image and also the isolation it gives up: the
#      GPU driver becomes reachable from untrusted code. The same doc has the threat
#      model and the controls that follow from it (dedicated fleet, one task per node).
#
# install-gvisor.sh fails the build if the base AMI's driver is not on nvproxy's list,
# so a bad pin cannot ship.

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
  # Same pin as the CPU AMI. Check `runsc nvproxy list-supported-drivers` before moving
  # it: a newer runsc does not necessarily support a newer driver, and vice versa.
  default = "20260803"
}

variable "nvidia_ami_release" {
  description = "EKS NVIDIA AMI release to pin (vYYYYMMDD), chosen for its NVIDIA driver version"
  type        = string
  # v20260810 ships driver 580.159.03, which gVisor 20260803 and 20260817 both support.
  # `recommended` (v20260818) ships 580.178.04, which neither supports. Verify a candidate
  # before changing this:
  #   gh api /repos/awslabs/amazon-eks-ami/releases/tags/<tag> --jq .body | grep -i nvidia
  default = "v20260810"
}

variable "subnet_filter_name" {
  description = "Name tag pattern for the build subnet; must have outbound internet (fetches the gVisor release)"
  type        = string
  default     = "*-vpc-public-*"
}

variable "build_instance_type" {
  description = "Instance type used only for the build (any x86_64 will do; no GPU required)"
  type        = string
  # No GPU needed: the build installs binaries, and the driver-support gate reads the
  # version out of the module file with modinfo rather than querying a device. Use a GPU
  # instance only if you add a live nvproxy smoke test to the build.
  default = "c7a.xlarge"
}

locals {
  ami_name = "osdc-ai-sandbox-gpu-gvisor-k8s${var.k8s_version}-${var.gvisor_release}-${formatdate("YYYYMMDDhhmmss", timestamp())}"
}

# Pinned EKS NVIDIA AMI — an exact release, not `recommended`, because the driver version
# is the thing being pinned.
data "amazon-parameterstore" "eks_al2023_nvidia" {
  name   = "/aws/service/eks/optimized-ami/${var.k8s_version}/amazon-linux-2023/x86_64/nvidia/amazon-eks-node-al2023-x86_64-nvidia-${var.k8s_version}-${var.nvidia_ami_release}/image_id"
  region = var.aws_region
}

source "amazon-ebs" "ai_sandbox_gpu" {
  region          = var.aws_region
  source_ami      = data.amazon-parameterstore.eks_al2023_nvidia.value
  instance_type   = var.build_instance_type
  ssh_username    = "ec2-user"
  ami_name        = local.ami_name
  ami_description = "EKS AL2023 NVIDIA + gVisor ${var.gvisor_release} with nvproxy, for the OSDC ai-sandbox-gpu fleet"

  # metadata_options: an org SCP denies RunInstances without http_tokens=required.
  # imds_support: the AMI itself must be v2-only, or a compromised agent could reach the
  # node role over IMDSv1 despite the hop limit.
  imds_support = "v2.0"
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  # Public subnet: no usable default VPC, and the build fetches the gVisor release.
  subnet_filter {
    filters = {
      "tag:Name" = "${var.cluster_name}${var.subnet_filter_name}"
    }
    most_free = true
    random    = false
  }
  associate_public_ip_address = true

  # Otherwise Packer opens :22 to 0.0.0.0/0 for the build. The address comes from
  # checkip.amazonaws.com, so if the builder's SSH egress differs (NAT pool, VPN)
  # SSH hangs — replace this line with temporary_security_group_source_cidrs =
  # ["x.x.x.x/32"]. Both are builder settings, not -var inputs, and setting both
  # is an error.
  temporary_security_group_source_public_ip = true

  # Karpenter selects by tag (defs/ai-sandbox-gpu.yaml), so the name is free to change.
  # NvidiaAmiRelease is recorded because it determines the driver version, which is the
  # thing that has to stay on nvproxy's supported list — the AMI date alone does not say.
  tags = {
    Name             = local.ami_name
    "osdc.io/ami"    = "ai-sandbox-gpu-gvisor"
    "osdc.io/module" = "nodepools-agent-sandbox"
    Cluster          = var.cluster_name
    GvisorRelease    = var.gvisor_release
    K8sVersion       = var.k8s_version
    NvidiaAmiRelease = var.nvidia_ami_release
    SourceAMI        = data.amazon-parameterstore.eks_al2023_nvidia.value
    Project          = "ciforge"
  }
  snapshot_tags = {
    Name          = local.ami_name
    "osdc.io/ami" = "ai-sandbox-gpu-gvisor"
  }
}

build {
  name    = "ai-sandbox-gpu-gvisor"
  sources = ["source.amazon-ebs.ai_sandbox_gpu"]

  provisioner "shell" {
    script = "${path.root}/scripts/install-gvisor.sh"
    environment_vars = [
      "GVISOR_RELEASE=${var.gvisor_release}",
      "ENABLE_NVPROXY=1",
    ]
    # {{ .Vars }} is required — overriding execute_command drops environment_vars.
    execute_command = "{{ .Vars }} sudo -E bash -eux '{{ .Path }}'"
  }

  post-processor "manifest" {
    output     = "${path.root}/manifest-gpu.json"
    strip_path = true
  }
}
