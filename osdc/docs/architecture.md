# Architecture

## Overview

OSDC (Open Source Dev Cloud) is a modular platform for deploying Kubernetes-based infrastructure on AWS EKS. It separates shared cluster infrastructure ("base") from optional services ("modules"), allowing multiple independent projects to share the same deployment tooling and base cluster without coupling to each other.

## Core Concepts

### Base

Every cluster gets the same base infrastructure:

- **VPC** — public/private subnets, NAT gateways, route tables
- **EKS** — managed Kubernetes cluster with OIDC, addons (vpc-cni, coredns, kube-proxy, ebs-csi), fixed-size base node group
- **Harbor** — S3 bucket, IAM roles/user for pull-through container image cache
- **Base k8s resources** — gp3 StorageClass, NVIDIA device plugin, node performance tuning DaemonSet, git-cache-warmer DaemonSet, Harbor namespace

The base terraform is a single parameterized root — no per-environment directories. Variables flow from `clusters.yaml` through the justfile as `-var` flags.

### Modules

Optional services layered on top of a base cluster. Each module is self-contained under `modules/<name>/` and may include:

- **terraform/** — independent tofu root for AWS resources (gets its own state key)
- **kubernetes/** — kustomize manifests
- **helm/** — values files for external Helm charts
- **deploy.sh** — custom deploy script called with `(cluster-id, cluster-name, region)`

Current modules:

| Module | Purpose |
|--------|---------|
| `karpenter` | Karpenter controller — IAM roles, SQS interruption queue, EventBridge rules, Helm install |
| `arc` | GitHub Actions Runner Controller — installs the ARC Helm chart |
| `nodepools` | Karpenter NodePools — pure compute provisioning (one NodePool per instance type) |
| `arc-runners` | ARC runner scale sets — GitHub Actions self-hosted runners (requires `arc` + `nodepools`) |
| `buildkit` | Container build service — dual-arch BuildKit Deployments on dedicated nodes |

Future modules (developed by other teams):

| Module | Purpose |
|--------|---------|
| `devgpu` | Dev GPU allocation — request GPU pods as dev instances |
| `remoteexec` | Remote execution — run jobs on remote server pods |

### clusters.yaml

The single source of truth for what gets deployed where:

```yaml
clusters:
  arc-staging:
    region: us-west-1
    cluster_name: pytorch-arc-staging
    state_bucket: ciforge-tfstate-arc-staging
    base:
      vpc_cidr: "10.0.0.0/16"
      single_nat_gateway: true
      base_node_count: 5
    modules:
      - arc
      - nodepools
      - arc-runners
      - buildkit
```

Adding a cluster = adding an entry. Adding a module to a cluster = appending to the `modules` list.

## Deployment Flow

```
just deploy <cluster-id>
│
├── deploy-base
│   ├── tofu apply (modules/eks/terraform/)  ← VPC, EKS, Harbor S3
│   ├── mirror-images                       ← Harbor images to ECR
│   ├── deploy-harbor                       ← Helm install Harbor
│   └── kubectl apply -k base/kubernetes/   ← StorageClass, NVIDIA, git-cache, etc.
│
└── deploy-module (for each module in order)
    ├── tofu apply (modules/<mod>/terraform/)   ← if exists
    ├── kubectl apply -k (modules/<mod>/kubernetes/)  ← if exists
    └── modules/<mod>/deploy.sh                 ← if exists
```

## Terraform State Architecture

```
S3 bucket: ciforge-tfstate-<cluster-id>
├── <cluster-id>/base/terraform.tfstate          ← base infra
├── <cluster-id>/arc/terraform.tfstate            ← arc module (if it has tf)
├── <cluster-id>/nodepools/terraform.tfstate       ← nodepools module (if it has tf)
├── <cluster-id>/arc-runners/terraform.tfstate    ← arc-runners module (if it has tf)
└── ...
```

- One S3 bucket per cluster, one state file per module
- DynamoDB lock table `ciforge-terraform-locks` is shared across all clusters
- State buckets live in us-west-2 regardless of cluster region
- Created via `scripts/bootstrap-state.sh`

## Why These Design Decisions

### Harbor always-on (not a module)

Every cluster needs a pull-through image cache. Without it, nodes pull directly from Docker Hub / ghcr.io / etc., hitting rate limits and adding latency. Harbor is foundational infrastructure, not optional.

### Git cache + NVIDIA plugin in base

These are universally needed. Any cluster running GPU workloads needs the NVIDIA plugin. Any cluster cloning repos benefits from git cache. Moving them to modules would mean every cluster config has to remember to include them.

### Single terraform root, parameterized

The old approach had `terraform/environments/staging/main.tf` and `terraform/environments/production/main.tf` — same code, different values. This is copy-paste maintenance burden. The new approach has one `modules/eks/terraform/main.tf` with variables. Cluster-specific values come from `clusters.yaml` as `-var` flags. Adding a cluster means adding a YAML entry, not duplicating terraform files.

### Compute split: nodepools + arc-runners

Compute provisioning (`nodepools`) is separate from GitHub Actions runners (`arc-runners`). The `nodepools` module deploys pure Karpenter NodePools — one per instance type. The `arc-runners` module deploys ARC runner scale sets and requires both `arc` (controller) and `nodepools` (compute). This lets a cluster like `remoteexec` get NodePools without needing the full GitHub Actions runner stack.

### Independent module terraform

Each module can have its own `terraform/` with a separate state file. This means:
- Modules can be added/removed without touching base state
- Module terraform changes don't require base re-plan
- Future projects (devgpu, remoteexec) can bring their own AWS resources without polluting base

### justfile as the single entry point

All state-changing operations go through `just`. This provides:
- Consistent deployment ordering
- Automatic cluster config resolution from `clusters.yaml`
- Protection against manual mistakes (wrong cluster, wrong order)
- Single place to audit what operations are available
