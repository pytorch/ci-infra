# Operations

## Prerequisites

- AWS CLI configured with appropriate credentials
- `mise` installed ([mise.jdx.dev](https://mise.jdx.dev))
- Working directory: `osdc/`

`mise` auto-installs all other tools (tofu, kubectl, helm, crane, etc.) on first run.

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
just deploy-module arc-staging arc
just deploy-module arc-staging nodepools
just deploy-module arc-staging arc-runners
just deploy-module arc-staging buildkit
```

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
kubectl get nodepools
kubectl get autoscalingrunnersets -n arc-runners
helm list -A
```

## Adding a new cluster

1. Pick a cluster ID, region, VPC CIDR, and decide which modules:

   ```yaml
   # clusters.yaml
   clusters:
     devgpu-prod:
       region: us-east-1
       cluster_name: pytorch-devgpu-production
       state_bucket: ciforge-tfstate-devgpu-prod
       base:
         vpc_cidr: "10.2.0.0/16"
         base_node_count: 3
       modules:
         - devgpu
   ```

2. Bootstrap state:
   ```bash
   just bootstrap devgpu-prod
   ```

3. Deploy:
   ```bash
   just deploy devgpu-prod
   ```

## Adding a new module

See [modules.md](modules.md) for the full guide. Quick version:

1. `mkdir -p modules/mymodule`
2. Add `deploy.sh` (and optionally `terraform/`, `kubernetes/`)
3. Add module name to target cluster's `modules` list in `clusters.yaml`
4. `just deploy-module <cluster> mymodule`

## Adding a new runner type

Two modules need definitions: `nodepools` (compute) and `arc-runners` (ARC scale set).

1. Ensure a NodePool exists for the instance type in `modules/nodepools/defs/`:
   ```yaml
   # modules/nodepools/defs/c6i-12xlarge.yaml
   nodepool:
     name: c6i-12xlarge
     instance_type: c6i.12xlarge
     arch: amd64
     disk_size: 100
     gpu: false
   ```

2. Create runner definition in `modules/arc-runners/defs/<name>.yaml`:
   ```yaml
   runner:
     name: my-runner
     instance_type: c6i.12xlarge    # must match a NodePool
     disk_size: 100
     vcpu: 8
     memory: 16Gi
     gpu: 0
   ```

3. Deploy:
   ```bash
   just deploy-module arc-staging nodepools
   just deploy-module arc-staging arc-runners
   ```

## Adding a cached git repository

The git cache uses a two-tier architecture: a central Deployment clones repos from GitHub and serves them via rsync, while a DaemonSet on each node syncs locally.

1. Edit the `REPOS` list in the `central.py` script inside `modules/eks/kubernetes/git-cache/central-configmap.yaml`
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
