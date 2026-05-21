# GPU Developer Servers Infrastructure
# Default: us-west-1 with 2x T4 instances (test environment)
# Production: Use -var-file="prod.tfvars" for us-east-2 with A100 instances

terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.23"
    }
    helm = {
      source  = "hashicorp/helm"
      version = "~> 2.12"
    }
    tls = {
      source  = "hashicorp/tls"
      version = "~> 4.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.4"
    }
  }
}

provider "aws" {
  region = local.current_config.aws_region
}

# Configure Kubernetes provider to use the EKS cluster (back to original approach for now)
provider "kubernetes" {
  host                   = aws_eks_cluster.gpu_dev_cluster.endpoint
  cluster_ca_certificate = base64decode(aws_eks_cluster.gpu_dev_cluster.certificate_authority[0].data)
  exec {
    api_version = "client.authentication.k8s.io/v1beta1"
    command     = "aws"
    args        = ["eks", "get-token", "--cluster-name", aws_eks_cluster.gpu_dev_cluster.name, "--region", local.current_config.aws_region]
  }
}

# Configure Helm provider to use the same EKS cluster
provider "helm" {
  kubernetes {
    host                   = aws_eks_cluster.gpu_dev_cluster.endpoint
    cluster_ca_certificate = base64decode(aws_eks_cluster.gpu_dev_cluster.certificate_authority[0].data)
    exec {
      api_version = "client.authentication.k8s.io/v1beta1"
      command     = "aws"
      args        = ["eks", "get-token", "--cluster-name", aws_eks_cluster.gpu_dev_cluster.name, "--region", local.current_config.aws_region]
    }
  }
}

# Data sources
data "aws_availability_zones" "available" {
  state = "available"
  # Exclude Local Zones (e.g. us-east-1-dfw-2a) and Wavelength Zones — EKS control
  # plane only supports standard AZs. us-east-2 doesn't have Local Zones so the
  # existing prod workspace was unaffected; us-east-1 has several (dfw, bos, …).
  filter {
    name   = "opt-in-status"
    values = ["opt-in-not-required"]
  }
}

data "aws_caller_identity" "current" {}

# Create workspace-specific prefix for global resources (IAM roles, etc.)
locals {
  workspace_prefix = "${var.prefix}-${terraform.workspace}"

  # Workspace-specific configurations
  workspace_configs = {
    default = {
      aws_region             = "us-west-1"
      environment            = "test"
      domain_name            = "test.devservers.io"
      gpu_instance_count     = 2
      use_self_managed_nodes = true
      instance_type          = "g4dn.12xlarge"
      supported_gpu_types = {
        "cpu-arm" = {
          instance_type       = "c7g.4xlarge"
          instance_types      = null
          instance_count      = 1
          gpus_per_instance   = 0
          use_placement_group = false
          architecture        = "arm64"
          efa_network_cards   = 0
        }
        "cpu-x86" = {
          instance_type       = "c7i.4xlarge"
          instance_types      = null
          instance_count      = 1
          gpus_per_instance   = 0
          use_placement_group = false
          architecture        = "x86_64"
          efa_network_cards   = 0
        }
        "t4" = {
          instance_type       = "g4dn.12xlarge"
          instance_types      = null
          instance_count      = 1
          gpus_per_instance   = 4
          use_placement_group = true
          architecture        = "x86_64"
          efa_network_cards   = 0
        }
        "t4-az2" = {
          instance_type       = "g4dn.12xlarge"
          instance_types      = null
          instance_count      = 0 # Disabled - use primary AZ only for testing
          gpus_per_instance   = 4
          use_placement_group = true
          architecture        = "x86_64"
          efa_network_cards   = 0
        }
        "h100" = {
          instance_type       = "p5.48xlarge"
          instance_types      = null
          instance_count      = 0 # Disabled - only use via CR when needed
          gpus_per_instance   = 8
          use_placement_group = false
          architecture        = "x86_64"
          efa_network_cards   = 32
        }
        "t4-small" = {
          instance_type       = "g4dn.2xlarge"
          instance_types      = null
          instance_count      = 0 # Disabled
          gpus_per_instance   = 1
          use_placement_group = false
          architecture        = "x86_64"
          efa_network_cards   = 0
        }
        # Note: Nsight profiling nodes are not separate ASGs - just label existing nodes:
        # kubectl label node <node-name> gpu.monitoring/profiling-dedicated=true nvidia.com/gpu.deploy.dcgm-exporter=false --overwrite
      }
    }
    prod = {
      aws_region             = "us-east-2"
      environment            = "prod"
      domain_name            = "devservers.io"
      gpu_instance_count     = 2
      use_self_managed_nodes = true
      instance_type          = "p4d.24xlarge"
      supported_gpu_types = {
        "b200" = {
          instance_type       = "p6-b200.48xlarge"
          instance_types      = null
          instance_count      = 2 # Fallback default (not used when capacity_reservations defined)
          gpus_per_instance   = 8
          use_placement_group = false
          architecture        = "x86_64"
          efa_network_cards   = 8 # p6-b200.48xlarge supports max 8 network cards
        }
        "h200" = {
          instance_type       = "p5en.48xlarge" # Match capacity reservation type
          instance_types      = ["p5e.48xlarge", "p5en.48xlarge"]
          instance_count      = 4 # Fallback default (not used when capacity_reservations defined)
          gpus_per_instance   = 8
          use_placement_group = false
          architecture        = "x86_64"
          efa_network_cards   = 32
        }
        "h100" = {
          instance_type       = "p5.48xlarge"
          instance_types      = null
          instance_count      = 2 # Fallback default (not used when capacity_reservations defined)
          gpus_per_instance   = 8
          use_placement_group = false
          architecture        = "x86_64"
          efa_network_cards   = 32
        }
        # Note: Nsight profiling nodes are not separate ASGs - just label existing nodes:
        # kubectl label node <node-name> gpu.monitoring/profiling-dedicated=true nvidia.com/gpu.deploy.dcgm-exporter=false --overwrite
        "a100" = {
          instance_type       = "p4d.24xlarge"
          instance_types      = null
          instance_count      = 2 # Fallback default (not used when capacity_reservations defined)
          gpus_per_instance   = 8
          use_placement_group = false
          architecture        = "x86_64"
          efa_network_cards   = 4
        }
        "t4" = {
          instance_type       = "g4dn.12xlarge"
          instance_types      = null
          instance_count      = 2
          gpus_per_instance   = 4
          use_placement_group = true
          architecture        = "x86_64"
          efa_network_cards   = 0
        }
        "l4" = {
          instance_type       = "g6.12xlarge"
          instance_types      = null
          instance_count      = 2
          gpus_per_instance   = 4 # 4x L4 GPUs
          use_placement_group = false
          architecture        = "x86_64"
          efa_network_cards   = 1
        }
        "a10g" = {
          instance_type       = "g5.12xlarge"
          instance_types      = null
          instance_count      = 1
          gpus_per_instance   = 4 # 4x A10G GPUs
          use_placement_group = false
          architecture        = "x86_64"
          efa_network_cards   = 1
        }
        "rtxpro6000" = {
          instance_type       = "g7e.24xlarge"
          instance_types      = null
          instance_count      = 2
          gpus_per_instance   = 4 # 4x RTX PRO 6000 Blackwell GPUs
          use_placement_group = false
          architecture        = "x86_64"
          efa_network_cards   = 2
        }
        # MIG slice SKUs — virtual: do NOT create an ASG. Surfaces the SKU to availability_updater
        # + reservation_processor. Backed by the H100 CR labelled with mig_profile=all-balanced
        # (per GPU = 2x1g.10gb + 1x2g.20gb + 1x3g.40gb).
        "h100-mig-1g" = {
          instance_type       = null
          instance_types      = null
          instance_count      = 0
          gpus_per_instance   = 16 # 8 GPUs * 2 slices/GPU
          use_placement_group = false
          architecture        = "x86_64"
          efa_network_cards   = 0
          virtual             = true
          k8s_resource        = "nvidia.com/mig-1g.10gb"
          node_gpu_type       = "h100"
        }
        "h100-mig-2g" = {
          instance_type       = null
          instance_types      = null
          instance_count      = 0
          gpus_per_instance   = 8 # 8 GPUs * 1 slice/GPU
          use_placement_group = false
          architecture        = "x86_64"
          efa_network_cards   = 0
          virtual             = true
          k8s_resource        = "nvidia.com/mig-2g.20gb"
          node_gpu_type       = "h100"
        }
        "h100-mig-3g" = {
          instance_type       = null
          instance_types      = null
          instance_count      = 0
          gpus_per_instance   = 8 # 8 GPUs * 1 slice/GPU
          use_placement_group = false
          architecture        = "x86_64"
          efa_network_cards   = 0
          virtual             = true
          k8s_resource        = "nvidia.com/mig-3g.40gb"
          node_gpu_type       = "h100"
        }
        # B200 MIG slices — virtual SKUs backed by ONE B200 node labelled with the custom
        # mig_profile "b200-6full-2mig-balanced": GPUs 0-5 stay as full B200 (still reservable
        # via --gpu-type b200), GPUs 6-7 get partitioned per-GPU into 2x1g.23gb + 1x2g.45gb +
        # 1x3g.90gb. Per node: 6 full + 4 small + 2 medium + 2 large slices.
        "b200-mig-1g" = {
          instance_type       = null
          instance_types      = null
          instance_count      = 0
          gpus_per_instance   = 4 # 2 partitioned GPUs * 2 slices each
          use_placement_group = false
          architecture        = "x86_64"
          efa_network_cards   = 0
          virtual             = true
          k8s_resource        = "nvidia.com/mig-1g.23gb"
          node_gpu_type       = "b200"
        }
        "b200-mig-2g" = {
          instance_type       = null
          instance_types      = null
          instance_count      = 0
          gpus_per_instance   = 2 # 2 partitioned GPUs * 1 slice each
          use_placement_group = false
          architecture        = "x86_64"
          efa_network_cards   = 0
          virtual             = true
          k8s_resource        = "nvidia.com/mig-2g.45gb"
          node_gpu_type       = "b200"
        }
        "b200-mig-3g" = {
          instance_type       = null
          instance_types      = null
          instance_count      = 0
          gpus_per_instance   = 2 # 2 partitioned GPUs * 1 slice each
          use_placement_group = false
          architecture        = "x86_64"
          efa_network_cards   = 0
          virtual             = true
          k8s_resource        = "nvidia.com/mig-3g.90gb"
          node_gpu_type       = "b200"
        }
        "cpu-arm" = {
          instance_type       = "c7g.8xlarge"
          instance_types      = null
          instance_count      = 10
          gpus_per_instance   = 0
          use_placement_group = false
          architecture        = "arm64"
          efa_network_cards   = 0
        }
        "cpu-x86" = {
          instance_type       = "c7i.8xlarge"
          instance_types      = null
          instance_count      = 10
          gpus_per_instance   = 0
          use_placement_group = false
          architecture        = "x86_64"
          efa_network_cards   = 0
        }
      }
    }
    # us-east-1 spot-only experimental cluster.
    # Same provisioning shape as prod (managed via the terraform.workspace switch) but
    # backed entirely by EC2 Spot — first cheap-and-cheerful environment we can deploy
    # new instance types into (B300 land here once on-demand quota arrives).
    "prod-east1" = {
      aws_region             = "us-east-1"
      environment            = "prod-east1"
      domain_name            = "east1.devservers.io"
      gpu_instance_count     = 1
      use_self_managed_nodes = true
      instance_type          = "g4dn.12xlarge"
      supported_gpu_types = {
        # 8-GPU spot instances. instance_count=1 means the ASG tries to maintain 1
        # spot instance per type — if AWS can't grant it (capacity / quota), the ASG
        # sits at 0 and gpu-dev reservations queue. Bump counts once we see what
        # actually gets fulfilled in us-east-1.
        "b300" = {
          instance_type       = "p6-b300.48xlarge"
          instance_types      = null
          instance_count      = 0
          gpus_per_instance   = 8
          use_placement_group = false
          architecture        = "x86_64"
          efa_network_cards   = 0  # EFA disabled until quota approved for p6-b300 in us-east-1
          use_spot            = true
        }
        "b200" = {
          instance_type       = "p6-b200.48xlarge"
          instance_types      = null
          instance_count      = 0
          gpus_per_instance   = 8
          use_placement_group = false
          architecture        = "x86_64"
          efa_network_cards   = 8
          use_spot            = true
        }
        "h200" = {
          instance_type       = "p5e.48xlarge"
          instance_types      = null
          instance_count      = 0
          gpus_per_instance   = 8
          use_placement_group = false
          architecture        = "x86_64"
          efa_network_cards   = 16
          use_spot            = true
        }
        "h100" = {
          instance_type       = "p5.48xlarge"
          instance_types      = null
          instance_count      = 0
          gpus_per_instance   = 8
          use_placement_group = false
          architecture        = "x86_64"
          efa_network_cards   = 32
          use_spot            = true
        }
        "a100" = {
          instance_type       = "p4d.24xlarge"
          instance_types      = null
          instance_count      = 0
          gpus_per_instance   = 8
          use_placement_group = false
          architecture        = "x86_64"
          efa_network_cards   = 4
          use_spot            = true
        }
        "t4" = {
          instance_type       = "g4dn.12xlarge"
          instance_types      = null
          instance_count      = 0
          gpus_per_instance   = 4
          use_placement_group = false
          architecture        = "x86_64"
          efa_network_cards   = 0
          use_spot            = true
        }
        "l4" = {
          instance_type       = "g6.12xlarge"
          instance_types      = null
          instance_count      = 0
          gpus_per_instance   = 4
          use_placement_group = false
          architecture        = "x86_64"
          efa_network_cards   = 1
          use_spot            = true
        }
        "rtxpro6000" = {
          instance_type       = "g7e.24xlarge"
          instance_types      = null
          instance_count      = 0
          gpus_per_instance   = 4
          use_placement_group = false
          architecture        = "x86_64"
          efa_network_cards   = 2
          use_spot            = true
        }
        "cpu-x86" = {
          instance_type       = "c7i.8xlarge"
          instance_types      = null
          instance_count      = 5
          gpus_per_instance   = 0
          use_placement_group = false
          architecture        = "x86_64"
          efa_network_cards   = 0
        }
        "cpu-spot" = {
          instance_type       = "c7i.2xlarge"
          instance_types      = null
          instance_count      = 0
          gpus_per_instance   = 0
          use_placement_group = false
          architecture        = "x86_64"
          efa_network_cards   = 0
          use_spot            = true
        }
      }
    }
  }

  # Current workspace configuration
  current_config = local.workspace_configs[terraform.workspace]

  # Workspace-specific capacity reservations (with manual instance counts)
  capacity_reservations = {
    "prod-east1" = {
      # No capacity reservations — this workspace is spot-only.
    }
    default = {
      # Test environment capacity reservations
      # h100 = [
      #   { id = "cr-09f598e08ec509a0f", instance_count = 2 }  # Expired - commented out
      # ]
      # h200 = [
      #   { id = "cr-0c0a6073304dd5d03", instance_count = 1 }  # Expired - commented out
      # ]
      h100 = [
        { key = "cr0", id = "cr-04d3d1d84e127a562", instance_count = 2 }, # H100 reservation us-west-1c (starts Wed)
      ]
    }
    prod = {
      # Production environment capacity reservations
      # NOTE: 'key' must match existing ASG suffix to avoid destroy/recreate.
      # When removing a CR, delete the entry entirely - keys are stable, not index-based.
      a100 = [
        { key = "cr0", id = "cr-01cc0f00f28b095af", instance_count = 1 }, # A100 reservation (1 instance)
        { key = "cr1", id = null, instance_count = 1 },                   # A100 on-demand (1 instance)
      ]
      h100 = [
        { key = "cr0", id = "cr-0a3f49b96fe03ca04", instance_count = 4 }, # H100 reservation us-east-2c (p5.48xlarge)
        { key = "cr1", id = null, instance_count = 2 },                   # H100 on-demand (2 instances)
        { key = "cr2", id = "cr-044bc72b0a6b56062", instance_count = 4 }, # H100 reservation us-east-2a (4 instances)
        { key = "cr3", id = "cr-0211ea1e8d3a3c79e", instance_count = 0, mig_profile = "all-balanced" }, # H100 reservation us-east-2c (EXPIRED CR - disabled)
        { key = "cr4", id = null, instance_count = 1, mig_profile = "all-balanced" }, # H100 on-demand MIG node
      ]
      h200 = [
        { key = "cr0", id = "cr-0f6d0766f5d3339e6", instance_count = 2 }, # H200 capacity block (may be expired - keep to prevent ASG destroy)
        { key = "cr1", id = "cr-06c9c978dea756a26", instance_count = 3 }, # H200 reservation (3 instances)
        { key = "cr2", id = null, instance_count = 2 },                   # H200 on-demand (2 instances)
        { key = "cr3", id = "cr-02949f61f1a761b54", instance_count = 1, efa_network_cards = 16 }, # H200 reservation us-east-2a (1 instance, 8 GPUs, p5en.48xlarge max 16 EFA)
      ]
      b200 = [
        { key = "cr0", id = "cr-0c366fb8339a10f69", instance_count = 0 }, # B200 reservation us-east-2a (disabled - CR freed)
        { key = "cr1", id = "cr-08e7fee0b8dc3de5e", instance_count = 3 }, # B200 reservation (3 instances)
        { key = "cr2", id = null, instance_count = 2 },                   # B200 on-demand (2 instances)
        { key = "cr3", id = "cr-0f5f6bb30a8fe3c68", instance_count = 1 }, # B200 reservation us-east-2b (1 regular instance)
        { key = "cr4", id = "cr-0f5f6bb30a8fe3c68", instance_count = 1, mig_profile = "b200-6full-2mig-balanced" }, # B200 reservation us-east-2b (1 MIG instance, auto-labeled)
      ]
      # T4 and L4 don't have capacity reservations - managed via supported_gpu_types fallback
    }
  }

  # Workspace-specific GPU type to subnet mappings
  gpu_subnet_assignments = {
    "prod-east1" = {
      # All node types land in the primary subnet (us-east-1a). Multi-EFA types
      # (efa_network_cards > 1) automatically use the private subnet in the same AZ.
      # Specific instance types may not have capacity in us-east-1a — those ASGs will
      # sit at 0 until we widen to other AZs, that's expected for beta.
      b300       = "primary"
      b200       = "primary"
      h200       = "primary"
      h100       = "primary"
      a100       = "primary"
      t4         = "primary"
      l4         = "primary"
      rtxpro6000 = "primary"
      "cpu-x86"  = "primary"
      "cpu-spot" = "primary"
    }
    default = {
      # Test environment - T4 nodes in multiple AZs for testing
      t4         = "primary"   # T4 in us-west-1a (primary AZ)
      "t4-az2"   = "secondary" # T4 in us-west-1b (secondary AZ)
      "cpu-arm"  = "primary"
      "cpu-x86"  = "primary"
      "t4-small" = "secondary"
      h100       = "secondary" # us-west-1c for H100 capacity reservation
    }
    prod = {
      # Production environment subnet assignments
      b200      = "secondary"  # us-east-2b (on-demand B200 capacity available here; CR-based ASGs override via capacity_reservation_azs)
      h200      = "tertiary" # us-east-2c for H200 capacity reservation
      h100      = "tertiary" # us-east-2c for H100 capacity reservation
      a100      = "primary"
      t4        = "primary"
      l4        = "secondary"
      a10g       = "secondary"
      rtxpro6000 = "secondary"
      "cpu-arm" = "primary"
      "cpu-x86" = "primary"
    }
  }

  # Subdomain NS delegations to create in *this* workspace's parent zone. Lets
  # prod (which owns devservers.io) auto-publish NS records pointing at child zones
  # in other workspaces (prod-east1, future regions) without manual -var flags.
  # The NS values come from `tofu output devservers_name_servers` in the child
  # workspace once its hosted zone has been created.
  prod_subdomain_delegations = {
    prod = {
      "east1.devservers.io" = [
        "ns-1079.awsdns-06.org",
        "ns-1999.awsdns-57.co.uk",
        "ns-341.awsdns-42.com",
        "ns-624.awsdns-14.net",
      ]
    }
  }

  # Per-capacity-reservation AZ mappings (overrides gpu_subnet_assignments when CR is used)
  capacity_reservation_azs = {
    "prod-east1" = {
      # Empty — no CRs in this workspace.
    }
    default = {
      "cr-04d3d1d84e127a562" = "secondary" # us-west-1c
    }
    prod = {
      # B200 capacity reservations
      "cr-0c366fb8339a10f69" = "primary"   # us-east-2a
      "cr-0122dff5e01d566dc" = "secondary" # us-east-2b
      "cr-08e7fee0b8dc3de5e" = "secondary" # us-east-2b
      "cr-0f5f6bb30a8fe3c68" = "secondary" # us-east-2b
      # H200 capacity reservations
      "cr-0f6d0766f5d3339e6" = "tertiary" # us-east-2c (may be expired - kept to prevent ASG destroy)
      "cr-06c9c978dea756a26" = "tertiary"  # us-east-2c
      "cr-02949f61f1a761b54" = "primary"   # us-east-2a
      # H100 capacity reservations
      "cr-0a3f49b96fe03ca04" = "tertiary" # us-east-2c (p5.48xlarge)
      "cr-044bc72b0a6b56062" = "primary"  # us-east-2a (p5.48xlarge)
      "cr-0211ea1e8d3a3c79e" = "tertiary" # us-east-2c (p5.48xlarge, MIG-dedicated)
      # A100 capacity reservation
      "cr-01cc0f00f28b095af" = "primary" # us-east-2a
    }
  }
}


# VPC Configuration
resource "aws_vpc" "gpu_dev_vpc" {
  cidr_block           = var.vpc_cidr
  enable_dns_hostnames = true
  enable_dns_support   = true

  tags = {
    Name        = "${var.prefix}-gpu-dev-vpc"
    Environment = local.current_config.environment
  }
}

# Internet Gateway
resource "aws_internet_gateway" "gpu_dev_igw" {
  vpc_id = aws_vpc.gpu_dev_vpc.id

  tags = {
    Name        = "${var.prefix}-gpu-dev-igw"
    Environment = local.current_config.environment
  }
}

# Primary subnet for EFA requirements (GPU nodes)
resource "aws_subnet" "gpu_dev_subnet" {
  vpc_id                  = aws_vpc.gpu_dev_vpc.id
  cidr_block              = var.subnet_cidr
  availability_zone       = data.aws_availability_zones.available.names[0]
  map_public_ip_on_launch = true

  tags = {
    Name                                          = "${var.prefix}-gpu-dev-subnet"
    Environment                                   = local.current_config.environment
    "kubernetes.io/cluster/${var.prefix}-cluster" = "shared"
    "kubernetes.io/role/elb"                      = "1"
  }
}

# Secondary subnet for EKS control plane (different AZ)
resource "aws_subnet" "gpu_dev_subnet_secondary" {
  vpc_id                  = aws_vpc.gpu_dev_vpc.id
  cidr_block              = "10.0.16.0/20"
  availability_zone       = data.aws_availability_zones.available.names[1] # us-east-2b for control plane diversity
  map_public_ip_on_launch = true

  tags = {
    Name                                          = "${var.prefix}-gpu-dev-subnet-secondary"
    Environment                                   = local.current_config.environment
    "kubernetes.io/cluster/${var.prefix}-cluster" = "shared"
    "kubernetes.io/role/elb"                      = "1"
  }
}

# Tertiary subnet for H200 capacity reservation (us-east-2c) - only if 3rd AZ exists
resource "aws_subnet" "gpu_dev_subnet_tertiary" {
  count                   = length(data.aws_availability_zones.available.names) >= 3 ? 1 : 0
  vpc_id                  = aws_vpc.gpu_dev_vpc.id
  cidr_block              = "10.0.32.0/20"
  availability_zone       = data.aws_availability_zones.available.names[2] # us-east-2c for H200 capacity reservation
  map_public_ip_on_launch = true

  tags = {
    Name                                          = "${var.prefix}-gpu-dev-subnet-tertiary"
    Environment                                   = local.current_config.environment
    "kubernetes.io/cluster/${var.prefix}-cluster" = "shared"
    "kubernetes.io/role/elb"                      = "1"
  }
}

# Route table
resource "aws_route_table" "gpu_dev_rt" {
  vpc_id = aws_vpc.gpu_dev_vpc.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.gpu_dev_igw.id
  }

  tags = {
    Name        = "${var.prefix}-gpu-dev-rt"
    Environment = local.current_config.environment
  }
}

resource "aws_route_table_association" "gpu_dev_rta" {
  subnet_id      = aws_subnet.gpu_dev_subnet.id
  route_table_id = aws_route_table.gpu_dev_rt.id
}

resource "aws_route_table_association" "gpu_dev_rta_secondary" {
  subnet_id      = aws_subnet.gpu_dev_subnet_secondary.id
  route_table_id = aws_route_table.gpu_dev_rt.id
}

resource "aws_route_table_association" "gpu_dev_rta_tertiary" {
  count          = length(aws_subnet.gpu_dev_subnet_tertiary)
  subnet_id      = aws_subnet.gpu_dev_subnet_tertiary[0].id
  route_table_id = aws_route_table.gpu_dev_rt.id
}

# --- Private subnets + NAT Gateway for multi-EFA instances ---
# Instances with 32 EFA interfaces can't have associate_public_ip_address in the
# launch template, so they go in private subnets and use NAT for internet access.

resource "aws_eip" "nat" {
  domain = "vpc"

  tags = {
    Name        = "${var.prefix}-nat-eip"
    Environment = local.current_config.environment
  }
}

resource "aws_nat_gateway" "gpu_dev_nat" {
  allocation_id = aws_eip.nat.id
  subnet_id     = aws_subnet.gpu_dev_subnet_secondary.id # NAT GW lives in public secondary subnet (primary is full)

  tags = {
    Name        = "${var.prefix}-nat-gw"
    Environment = local.current_config.environment
  }

  depends_on = [aws_internet_gateway.gpu_dev_igw]
}

# Private route table: internet via NAT Gateway
resource "aws_route_table" "gpu_dev_private_rt" {
  vpc_id = aws_vpc.gpu_dev_vpc.id

  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.gpu_dev_nat.id
  }

  tags = {
    Name        = "${var.prefix}-gpu-dev-private-rt"
    Environment = local.current_config.environment
  }
}

# Private subnets mirroring the public ones (one per AZ)
resource "aws_subnet" "gpu_dev_private_subnet" {
  vpc_id                  = aws_vpc.gpu_dev_vpc.id
  cidr_block              = "10.0.48.0/20"
  availability_zone       = data.aws_availability_zones.available.names[0]
  map_public_ip_on_launch = false

  tags = {
    Name                                          = "${var.prefix}-gpu-dev-private-subnet"
    Environment                                   = local.current_config.environment
    "kubernetes.io/cluster/${var.prefix}-cluster" = "shared"
  }
}

resource "aws_subnet" "gpu_dev_private_subnet_secondary" {
  vpc_id                  = aws_vpc.gpu_dev_vpc.id
  cidr_block              = "10.0.64.0/20"
  availability_zone       = data.aws_availability_zones.available.names[1]
  map_public_ip_on_launch = false

  tags = {
    Name                                          = "${var.prefix}-gpu-dev-private-subnet-secondary"
    Environment                                   = local.current_config.environment
    "kubernetes.io/cluster/${var.prefix}-cluster" = "shared"
  }
}

resource "aws_subnet" "gpu_dev_private_subnet_tertiary" {
  count                   = length(data.aws_availability_zones.available.names) >= 3 ? 1 : 0
  vpc_id                  = aws_vpc.gpu_dev_vpc.id
  cidr_block              = "10.0.80.0/20"
  availability_zone       = data.aws_availability_zones.available.names[2]
  map_public_ip_on_launch = false

  tags = {
    Name                                          = "${var.prefix}-gpu-dev-private-subnet-tertiary"
    Environment                                   = local.current_config.environment
    "kubernetes.io/cluster/${var.prefix}-cluster" = "shared"
  }
}

resource "aws_route_table_association" "gpu_dev_private_rta" {
  subnet_id      = aws_subnet.gpu_dev_private_subnet.id
  route_table_id = aws_route_table.gpu_dev_private_rt.id
}

resource "aws_route_table_association" "gpu_dev_private_rta_secondary" {
  subnet_id      = aws_subnet.gpu_dev_private_subnet_secondary.id
  route_table_id = aws_route_table.gpu_dev_private_rt.id
}

resource "aws_route_table_association" "gpu_dev_private_rta_tertiary" {
  count          = length(aws_subnet.gpu_dev_private_subnet_tertiary)
  subnet_id      = aws_subnet.gpu_dev_private_subnet_tertiary[0].id
  route_table_id = aws_route_table.gpu_dev_private_rt.id
}

# Security Groups

# Control plane security group
resource "aws_security_group" "eks_control_plane_sg" {
  name        = "${var.prefix}-eks-control-plane-sg"
  description = "Security group for EKS control plane"
  vpc_id      = aws_vpc.gpu_dev_vpc.id

  # Allow inbound HTTPS from worker nodes (VPC CIDR)
  ingress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = [aws_vpc.gpu_dev_vpc.cidr_block]
    description = "HTTPS from worker nodes"
  }

  # Allow all outbound traffic
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name        = "${var.prefix}-eks-control-plane-sg"
    Environment = local.current_config.environment
  }
}

resource "aws_security_group" "gpu_dev_sg" {
  name        = "${var.prefix}-gpu-dev-sg"
  description = "Security group for GPU development servers"
  vpc_id      = aws_vpc.gpu_dev_vpc.id



  # Kubelet API for logs/exec/port-forward
  ingress {
    from_port   = 10250
    to_port     = 10250
    protocol    = "tcp"
    cidr_blocks = [aws_vpc.gpu_dev_vpc.cidr_block]
    description = "Kubelet API access from EKS control plane"
  }

  # HTTPS outbound to EKS control plane for CoreDNS and other system pods
  ingress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = [aws_vpc.gpu_dev_vpc.cidr_block]
    description = "HTTPS access to EKS control plane"
  }

  # DNS resolution for pods
  ingress {
    from_port   = 53
    to_port     = 53
    protocol    = "tcp"
    cidr_blocks = [aws_vpc.gpu_dev_vpc.cidr_block]
    description = "DNS TCP access within VPC"
  }

  ingress {
    from_port   = 53
    to_port     = 53
    protocol    = "udp"
    cidr_blocks = [aws_vpc.gpu_dev_vpc.cidr_block]
    description = "DNS UDP access within VPC"
  }

  # All traffic within security group for EFA (RDMA requires protocol -1, not just TCP)
  ingress {
    from_port = 0
    to_port   = 0
    protocol  = "-1"
    self      = true
  }

  # NodePort range for WebSocket SSH proxy ECS tasks
  dynamic "ingress" {
    for_each = local.effective_domain_name != "" ? [1] : []
    content {
      from_port       = 30000
      to_port         = 32767
      protocol        = "tcp"
      security_groups = [aws_security_group.ssh_proxy[0].id]
      description     = "NodePort range for SSH via WebSocket proxy ECS tasks"
    }
  }

  # NodePort range for Jupyter ALB access
  dynamic "ingress" {
    for_each = local.effective_domain_name != "" ? [1] : []
    content {
      from_port       = 30000
      to_port         = 32767
      protocol        = "tcp"
      security_groups = [aws_security_group.alb_sg[0].id]
      description     = "NodePort range for Jupyter Lab via ALB"
    }
  }

  # All outbound traffic
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  # EFA RDMA requires self-referencing egress (EFA-only interfaces have no IP,
  # so the CIDR-based rule above can't match their outbound traffic)
  egress {
    from_port = 0
    to_port   = 0
    protocol  = "-1"
    self      = true
  }

  tags = {
    Name        = "${var.prefix}-gpu-dev-sg"
    Environment = local.current_config.environment
  }
}

# Cluster Placement Groups - one per GPU type that needs placement groups
resource "aws_placement_group" "gpu_dev_pg" {
  for_each = {
    for gpu_type, config in local.current_config.supported_gpu_types : gpu_type => config
    if config.use_placement_group
  }

  name     = "${local.workspace_prefix}-gpu-${each.key}-cluster"
  strategy = "cluster"

  # Note: Placement group AZ will be determined by first instance launched

  tags = {
    Name        = "${local.workspace_prefix}-gpu-${each.key}-cluster"
    Environment = local.current_config.environment
    GpuType     = each.key
  }
}
