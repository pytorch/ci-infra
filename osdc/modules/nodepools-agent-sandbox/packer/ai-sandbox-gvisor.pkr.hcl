# EKS AL2023 AMI with gVisor (runsc) baked in, for the ai-sandbox fleet.
#
#   just build-agent-sandbox-ami <cluster>
#
# Baked rather than installed from userData: that fetched binaries on every
# scale-up and restarted containerd after kubelet was already running. runsc is
# registered instead via a nodeadm NodeConfig in /etc/eks/nodeadm.d/, merged
# before containerd starts — writing config.toml in the image wouldn't survive,
# nodeadm regenerates it each boot.
#
# Base AMI and gVisor are both pinned, so CVE fixes need a rebuild (README).

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
  # Pinned: `latest` meant nodes launched a day apart could differ.
  default = "20260803"
}

variable "subnet_filter_name" {
  description = "Name tag pattern for the build subnet; must have outbound internet (fetches the gVisor release)"
  type        = string
  default     = "*-vpc-public-*"
}

variable "build_instance_type" {
  description = "Instance type used only for the build (any x86_64 will do; matches the fleet's family)"
  type        = string
  default     = "c7a.xlarge"
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
  region          = var.aws_region
  source_ami      = data.amazon-parameterstore.eks_al2023.value
  instance_type   = var.build_instance_type
  ssh_username    = "ec2-user"
  ami_name        = local.ami_name
  ami_description = "EKS AL2023 + gVisor ${var.gvisor_release} for the OSDC ai-sandbox fleet"

  # metadata_options: an org SCP denies RunInstances without http_tokens=required.
  # imds_support: the AMI itself must be v2-only, or a compromised agent could
  # reach the node role over IMDSv1 despite the hop limit.
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
  # SSH hangs — pass -var 'temporary_security_group_source_cidrs=[...]' instead.
  temporary_security_group_source_public_ip = true

  # Karpenter selects by tag (defs/ai-sandbox.yaml), so the name is free to change.
  tags = {
    Name             = local.ami_name
    "osdc.io/ami"    = "ai-sandbox-gvisor"
    "osdc.io/module" = "nodepools-agent-sandbox"
    Cluster          = var.cluster_name
    GvisorRelease    = var.gvisor_release
    K8sVersion       = var.k8s_version
    SourceAMI        = data.amazon-parameterstore.eks_al2023.value
    Project          = "ciforge"
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
    # {{ .Vars }} is required — overriding execute_command drops environment_vars.
    execute_command = "{{ .Vars }} sudo -E bash -eux '{{ .Path }}'"
  }

  post-processor "manifest" {
    output     = "${path.root}/manifest.json"
    strip_path = true
  }
}
