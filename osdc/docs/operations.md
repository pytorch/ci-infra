# Operations

## Prerequisites

- AWS CLI configured with appropriate credentials
- `mise` installed ([mise.jdx.dev](https://mise.jdx.dev))
- `just` installed (not managed by `mise` — install separately)
- Working directory: `osdc/`

`mise` auto-installs all other tools (tofu, kubectl, helm, crane, awscli, packer, `uv`, plus linters) on first run. Run `just setup` once to create the Python virtualenv (`uv sync`) used by `cluster-config.py` and other helpers.

## First-time setup

### 1. Bootstrap state storage

Each cluster needs an S3 bucket for tofu state and a shared DynamoDB lock table.

```bash
# Single cluster
just bootstrap arc-staging

# All clusters defined in clusters.yaml
just bootstrap-all
```

Idempotent — safe to run multiple times.

### 2. Deploy a cluster

```bash
# Full deploy (base + all modules)
just deploy arc-staging

# Or step by step
just deploy-base arc-staging
just deploy-module arc-staging karpenter
just deploy-module arc-staging arc
just deploy-module arc-staging nodepools
just deploy-module arc-staging arc-runners
just deploy-module arc-staging buildkit
just deploy-module arc-staging pypi-cache
just deploy-module arc-staging cache-enforcer
just deploy-module arc-staging zombie-cleanup
just deploy-module arc-staging harbor-cache-recovery
just deploy-module arc-staging monitoring
just deploy-module arc-staging logging
```

The full per-cluster module list lives in `clusters.yaml` under `clusters.<id>.modules`. Production clusters add GPU pools (`nodepools-h100`, `nodepools-b200`) and matching runners (`arc-runners-h100`, `arc-runners-b200`).

## Day-to-day operations

### Deploy a specific module

```bash
just deploy-module arc-staging nodepools
just deploy-module arc-staging arc-runners
```

### Inspect cluster state

```bash
just show arc-staging        # shows config, modules, tofu vars
just list                    # all clusters and their modules
```

### Read-only debugging

```bash
kubectl get nodes
kubectl get pods -A
kubectl get nodepools                                # requires the karpenter module
kubectl get autoscalingrunnersets -n arc-runners
helm list -A
```

## Adding a new cluster

1. Pick a cluster ID, region, VPC CIDR, and decide which modules. The VPC CIDR sizes only the IPv4 footprint (nodes, NAT, ENI primary IPs) — pod IPs come from an AWS-allocated /56 IPv6 block, so a `/16` is more than enough even for very large fleets. Module names must match a directory under `modules/` — see the existing `arc-staging` and `arc-cbr-production` entries in `clusters.yaml` for full, working examples. Minimal skeleton:

   ```yaml
   # clusters.yaml
   clusters:
     my-new-cluster:
       region: us-east-1
       cluster_name: pytorch-my-new-cluster
       state_bucket: ciforge-tfstate-my-new-cluster
       base:
         vpc_cidr: "10.2.0.0/16"
         base_node_count: 3
       modules:
         - karpenter
         - arc
         - nodepools
         - arc-runners
         - buildkit
   ```

2. Bootstrap state:
   ```bash
   just bootstrap my-new-cluster
   ```

3. Deploy:
   ```bash
   just deploy my-new-cluster
   ```

## Cluster lifecycle

For destroying and recreating an existing cluster (e.g. for the IPv4 → IPv6 migration, since EKS `ip_family` is immutable post-creation), see [`ipv6-cluster-recreation.md`](ipv6-cluster-recreation.md).

## Adding a new module

See [modules.md](modules.md) for the full guide. Quick version:

1. `mkdir -p modules/mymodule`
2. Add `deploy.sh` (and optionally `terraform/`, `kubernetes/`)
3. Add module name to target cluster's `modules` list in `clusters.yaml`
4. `just deploy-module <cluster> mymodule`

## Adding a new runner type

Two modules need definitions: `nodepools` (compute) and `arc-runners` (ARC scale set).

1. Ensure a NodePool fleet exists that covers the instance type in `modules/nodepools/defs/`. Files are named after the **fleet** (e.g. `c7i.yaml`, `m7i.yaml`, `g5.yaml`), not per-instance-type. A fleet contains an ordered `instances:` list and Karpenter picks the best fit:

   ```yaml
   # modules/nodepools/defs/c7i.yaml
   fleet:
     name: c7i
     arch: amd64
     gpu: false
     instances:
       - type: c7i.48xlarge
         weight: 100
         node_disk_size: 3750
       - type: c7i.24xlarge
         weight: 85
         node_disk_size: 1900
       - type: c7i.12xlarge
         weight: 40
         node_disk_size: 1350
       - type: c7i.8xlarge
         weight: 20
         node_disk_size: 650
   ```

2. Create runner definition in `modules/arc-runners/defs/<name>.yaml`. Names follow the convention in [`runner_naming_convention.md`](runner_naming_convention.md) (`l-<arch><vendor><features>-<vcpu>-<memory>[-<gpu>[-<count>]]`). Both `proactive_capacity` and `max_burst_capacity` are required:

   ```yaml
   # modules/arc-runners/defs/l-x86iamx-8-16.yaml
   runner:
     name: l-x86iamx-8-16
     instance_type: c7i.12xlarge   # must match an instance type in a NodePool fleet
     disk_size: 150
     vcpu: 8
     memory: 16Gi
     gpu: 0
     proactive_capacity: 30        # minimum warm runners kept hot
     max_burst_capacity: 2000      # ceiling for ARC scale-up
   ```

3. Deploy:
   ```bash
   just deploy-module arc-staging nodepools
   just deploy-module arc-staging arc-runners
   ```

## Adding a cached git repository

The git cache uses a two-tier architecture: a central StatefulSet clones repos from GitHub and serves them via rsync, while a DaemonSet on each node syncs locally.

1. Edit the appropriate list in the `central.py` script inside `base/kubernetes/git-cache/central-configmap.yaml`. There are **two** lists — pick the right one:
   - `REPOS_FULL` — repos with submodules. Cloned non-bare with `--recurse-submodules` so `.git/modules/<name>/objects/` is available for `actions/checkout` submodule alternates. Currently: `pytorch/pytorch`.
   - `REPOS_BARE` — repos without submodules. Cloned bare (lightweight). Currently: `pytorch/test-infra`.

   Adding a submodule-bearing repo to `REPOS_BARE` will break checkout — when in doubt, use `REPOS_FULL`.

2. Redeploy:
   ```bash
   just deploy-base arc-staging
   ```

Note: Runner pods use `CHECKOUT_GIT_CACHE_DIR` (not `GIT_ALTERNATE_OBJECT_DIRECTORIES`) to find the cache. The `actions/checkout` action uses `reference-repository` to leverage the cache. No runner template changes are needed when adding a new repository.

## Terraform state management

State is in S3 with DynamoDB locking:

```
s3://ciforge-tfstate-<cluster-id>/<cluster-id>/base/terraform.tfstate
s3://ciforge-tfstate-<cluster-id>/<cluster-id>/<module>/terraform.tfstate
```

State buckets and the lock table always live in `us-west-2` regardless of the cluster's own region — this is a hardcoded constant (`STATE_REGION` in `scripts/bootstrap-state.sh` and the `justfile`). Don't change the `region=us-west-2` argument when initializing the backend, even for clusters in other regions.

To inspect state:
```bash
cd modules/eks/terraform
tofu init -reconfigure \
    -backend-config="bucket=ciforge-tfstate-arc-staging" \
    -backend-config="key=arc-staging/base/terraform.tfstate" \
    -backend-config="region=us-west-2" \
    -backend-config="dynamodb_table=ciforge-terraform-locks"
tofu state list
tofu output
```
