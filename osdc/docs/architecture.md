# Architecture

## Overview

OSDC (Open Source Dev Cloud) is a modular platform for deploying Kubernetes-based infrastructure on AWS EKS. It separates shared cluster infrastructure ("base") from optional services ("modules"), allowing multiple independent projects to share the same deployment tooling and base cluster without coupling to each other.

## Core Concepts

### Base

Every cluster gets the same base infrastructure:

- **VPC** — public/private subnets, NAT gateways, route tables
- **EKS** — managed Kubernetes cluster with OIDC, addons (vpc-cni, coredns, kube-proxy, ebs-csi), fixed-size base node group
- **Harbor** — S3 bucket, IAM roles/user for pull-through container image cache
- **Base k8s resources** — gp3 StorageClass, NVIDIA device plugin, node performance tuning DaemonSet, git-cache (two-tier: central StatefulSet + rsync DaemonSet), Harbor namespace, image-cache-janitor (prunes stale image content from node disks), NodeLocal DNSCache (per-node CoreDNS DaemonSet that intercepts pod DNS via iptables-mode NOTRACK)
- **Node compactor** — Taints underutilized Karpenter nodes for workload consolidation (configurable via `clusters.yaml`)
- **Karpenter** — installed as a module but required for the autoscaling story; deployed before any compute-provisioning module

The base terraform is a single parameterized root that lives at `modules/eks/terraform/` (the `base/terraform/` directory contains no `.tf` files — it is leftover state cruft and should be ignored). Variables flow from `clusters.yaml` through the justfile as `-var` flags.

### Modules

Optional services layered on top of a base cluster. Each module is self-contained under `modules/<name>/` and may include:

- **terraform/** — independent tofu root for AWS resources (gets its own state key)
- **kubernetes/** — kustomize manifests
- **helm/** — values files for external Helm charts
- **deploy.sh** — custom deploy script called with `(cluster-id, cluster-name, region)`

Note: `eks` is not a regular module — it is the base, deployed by `deploy-base` and never appears in any cluster's `modules:` list. Its source lives under `modules/eks/` for code organization but it follows the base contract (state key `<cluster>/base/terraform.tfstate`).

Current modules:

| Module | Purpose |
|--------|---------|
| `karpenter` | Karpenter controller — IAM roles, SQS interruption queue, EventBridge rules, Helm install (deployed first; required by all compute modules) |
| `arc` | GitHub Actions Runner Controller — installs the ARC Helm chart |
| `nodepools` | Karpenter NodePools generated from `defs/` — multi-flavor compute provisioning (CPU/GPU, runner vs build, metal variants) |
| `nodepools-b200` | B200 GPU NodePools — delegates to upstream nodepools deploy with B200-specific definitions |
| `nodepools-h100` | H100 GPU NodePools — delegates to upstream nodepools deploy with H100-specific definitions |
| `arc-runners` | ARC runner scale sets generated from `defs/` + templates — GitHub Actions self-hosted runners (requires `arc` + `nodepools`) |
| `arc-runners-b200` | B200 ARC runner scale sets — delegates to upstream arc-runners deploy with B200-specific definitions |
| `arc-runners-h100` | H100 ARC runner scale sets — delegates to upstream arc-runners deploy with H100-specific definitions |
| `buildkit` | Container build service — dual-arch BuildKit Deployments with HAProxy LB on dedicated nodes |
| `pypi-cache` | Per-CUDA-slug nginx + pypiserver fanout backed by shared EFS wheelhouse, fed by an external wheel-build pipeline via S3 |
| `cache-enforcer` | DaemonSet that installs iptables rules on runner nodes to block direct outbound access to external registries/PyPI, forcing traffic through internal caches |
| `zombie-cleanup` | CronJob that reaps stuck/zombie ARC runner pods (configurable max age for pending/running) |
| `harbor-cache-recovery` | CronJob that detects ImagePullBackOff from Harbor proxy cache corruption and purges stale cache entries |
| `logging` | Log collection pipeline — Grafana Alloy DaemonSet (pod logs + journal) + Events Deployment → Grafana Cloud Loki |
| `monitoring` | Metrics pipeline — kube-prometheus-stack CRDs/exporters + Grafana Alloy → Grafana Cloud Mimir (see `docs/observability.md` for the three-Alloy architecture) |

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
│   ├── tofu apply (modules/eks/terraform/)         ← VPC, EKS, Harbor S3
│   ├── mirror-images                               ← Harbor images to ECR
│   ├── kubectl apply -k base/kubernetes/           ← StorageClass, NVIDIA, image-cache-janitor, etc.
│   ├── git-cache/deploy.sh                         ← Git cache central StatefulSet
│   ├── eniconfigs/deploy.sh                          ← AZ-named ENIConfig CRs (one per AZ from terraform output)
│   ├── deploy-harbor                               ← Helm install Harbor (pull-through cache)
│   ├── node-compactor/deploy.sh                    ← if enabled in clusters.yaml
│   ├── image-cache-janitor/deploy.sh               ← prunes stale image content from node disks
│   └── nodelocaldns/deploy.sh                      ← per-node CoreDNS cache (resolves kube-dns ClusterIP at apply time)
│
└── deploy-module (for each module in order)
    ├── tofu apply (modules/<mod>/terraform/)        ← if exists
    ├── kubectl apply -k (modules/<mod>/kubernetes/) ← if exists
    └── modules/<mod>/deploy.sh                      ← if exists
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

Compute provisioning (`nodepools`) is separate from GitHub Actions runners (`arc-runners`). The `karpenter` module (controller) must be deployed first — it is what provisions nodes for any NodePool. The `nodepools` module then deploys Karpenter NodePools generated from per-instance-type definitions in `defs/`. The `arc-runners` module deploys ARC runner scale sets and requires both `arc` (controller) and `nodepools` (compute). This lets a cluster get NodePools without needing the full GitHub Actions runner stack.

### Independent module terraform

Each module can have its own `terraform/` with a separate state file. This means:
- Modules can be added/removed without touching base state
- Module terraform changes don't require base re-plan
- New projects can bring their own AWS resources without polluting base

### justfile as the single entry point

All state-changing operations go through `just`. This provides:
- Consistent deployment ordering
- Automatic cluster config resolution from `clusters.yaml`
- Protection against manual mistakes (wrong cluster, wrong order)
- Single place to audit what operations are available
