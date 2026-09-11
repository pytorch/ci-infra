# EKS AL2023 GPU AMI with a newer NVIDIA driver branch baked in, for the compute
# GPU fleets (p4d / p5 / p6-b200).
#
#   just build-gpu-driver-ami <cluster>
#
# The stock EKS GPU AMI ships driver 580 and cannot be built past 595 (AWS's
# GRID runfile bucket gates the version). CUDA 13.4's own feature set wants
# R615. This takes the finished EKS AMI and swaps the driver in place — see
# docs/nvidia-driver-615-ami.md for why that is the only route, and README.md
# here for what the swap touches.
#
# Same shape as modules/nodepools-agent-sandbox/packer: base AMI and driver are
# both pinned, so CVE fixes need a rebuild.

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

variable "nvidia_driver_version" {
  description = "Full NVIDIA driver version to install, from the cuda-amzn2023 repo"
  type        = string
  # R615 is the branch CUDA 13.4 asks for. Pinned to a full version rather than
  # a major: the repo serves several, and two nodes built a week apart should
  # not differ.
  default = "615.71.09"

  validation {
    # Below 615 the stock AMI or an upstream build is the right answer, and this
    # module's open-kmod-only assumptions do not hold.
    condition     = parseint(split(".", var.nvidia_driver_version)[0], 10) >= 615
    error_message = "This module only handles 615+; the proprietary-kmod path it removes still exists below that."
  }
}

variable "nvidia_gdrcopy_version" {
  description = "gdrcopy kmod version to rebuild against the new driver"
  type        = string
  # Matches nvidia_gdrcopy_driver_version in the upstream EKS AMI build, so the
  # node keeps the gdrdrv it would have had.
  default = "2.5.2"
}

variable "subnet_filter_name" {
  description = "Name tag pattern for the build subnet; must have outbound internet (fetches driver RPMs)"
  type        = string
  default     = "*-vpc-public-*"
}

variable "build_instance_type" {
  description = "Instance type used only for the build — DKMS compiles fine without a GPU, so this is deliberately not a p-family box"
  type        = string
  default     = "c7a.4xlarge"
}

locals {
  ami_name = "osdc-gpu-nvidia${var.nvidia_driver_version}-k8s${var.k8s_version}-${formatdate("YYYYMMDDhhmmss", timestamp())}"
}

# Latest EKS-optimized AL2023 *NVIDIA* AMI for this Kubernetes version. This is
# the same image the stock GPU pools resolve through their
# `amazon-eks-node-al2023-x86_64-nvidia-*` selector, so the only delta between a
# stock GPU node and one of these is the driver.
data "amazon-parameterstore" "eks_al2023_nvidia" {
  name   = "/aws/service/eks/optimized-ami/${var.k8s_version}/amazon-linux-2023/x86_64/nvidia/recommended/image_id"
  region = var.aws_region
}

source "amazon-ebs" "gpu_nvidia" {
  region          = var.aws_region
  source_ami      = data.amazon-parameterstore.eks_al2023_nvidia.value
  instance_type   = var.build_instance_type
  ssh_username    = "ec2-user"
  ami_name        = local.ami_name
  ami_description = "EKS AL2023 GPU + NVIDIA ${var.nvidia_driver_version} for the OSDC compute GPU fleets"

  # The GPU AMI's root volume is larger than the default, and DKMS builds three
  # module trees before archiving them.
  launch_block_device_mappings {
    device_name           = "/dev/xvda"
    volume_size           = 100
    volume_type           = "gp3"
    delete_on_termination = true
  }

  # metadata_options: an org SCP denies RunInstances without http_tokens=required.
  imds_support = "v2.0"
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  # Public subnet: no usable default VPC, and the build pulls driver RPMs from
  # developer.download.nvidia.com.
  subnet_filter {
    filters = {
      "tag:Name" = "${var.cluster_name}${var.subnet_filter_name}"
    }
    most_free = true
    random    = false
  }
  associate_public_ip_address = true

  # Otherwise Packer opens :22 to 0.0.0.0/0 for the build. See the note in
  # modules/nodepools-agent-sandbox/packer if SSH hangs behind a NAT pool or VPN.
  temporary_security_group_source_public_ip = true

  # Karpenter selects by tag, so the name is free to change. NvidiaDriver is the
  # tag to read when auditing which driver a fleet is actually on.
  tags = {
    Name             = local.ami_name
    "osdc.io/ami"    = "gpu-nvidia-615"
    "osdc.io/module" = "nodepools"
    Cluster          = var.cluster_name
    NvidiaDriver     = var.nvidia_driver_version
    GdrcopyVersion   = var.nvidia_gdrcopy_version
    K8sVersion       = var.k8s_version
    SourceAMI        = data.amazon-parameterstore.eks_al2023_nvidia.value
    Project          = "ciforge"
  }
  snapshot_tags = {
    Name          = local.ami_name
    "osdc.io/ami" = "gpu-nvidia-615"
  }
}

build {
  name    = "gpu-nvidia-driver"
  sources = ["source.amazon-ebs.gpu_nvidia"]

  provisioner "shell" {
    script = "${path.root}/scripts/install-nvidia-driver.sh"
    environment_vars = [
      "NVIDIA_DRIVER_VERSION=${var.nvidia_driver_version}",
      "NVIDIA_GDRCOPY_VERSION=${var.nvidia_gdrcopy_version}",
    ]
    # {{ .Vars }} is required — overriding execute_command drops environment_vars.
    execute_command = "{{ .Vars }} sudo -E bash -eux '{{ .Path }}'"
  }

  post-processor "manifest" {
    output     = "${path.root}/manifest.json"
    strip_path = true
  }
}
