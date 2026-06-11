# Operations

## Prerequisites

- AWS CLI configured with credentials for an IAM principal mapped to one of the roles in the target cluster's `access_config.cluster_admin_role_names` (e.g. `osdc_gha_staging` for `arc-staging`, `osdc_gha_prod` for `arc-cbr-production`). How those credentials are acquired (SSO, role assumption, static keys) is left to the operator's organization — the project does not prescribe a profile or login flow.
- `mise` installed ([mise.jdx.dev](https://mise.jdx.dev))
- `just` installed (not managed by `mise` — install separately)
- Working directory: `osdc/`

`mise` auto-installs all other tools (tofu, kubectl, helm, crane, awscli, packer, `uv`, plus linters) on first run. Run `just setup` once to create the Python virtualenv (`uv sync`) used by `cluster-config.py` and other helpers.

`mise` also exports `AWS_REGION=us-west-2` for every shell rooted at the project. This is the state-bucket region (state and the lock table always live in `us-west-2`) and the default for `aws` calls; override `AWS_REGION` explicitly when running ad-hoc `aws` commands against another cluster's region (e.g. `arc-cbr-production` lives in `us-east-2`).

### Corporate proxy (Meta and similar)

In environments behind an HTTP proxy that does not whitelist `*.eks.amazonaws.com`, `aws eks ...`, `kubectl`, and `helm` calls will hang or time out. Extend the bypass list:

```bash
export NO_PROXY="${NO_PROXY:-},.eks.amazonaws.com"
export no_proxy="${no_proxy:-},.eks.amazonaws.com"
```

Every `just` recipe that touches the EKS API inlines this bypass for you. The export above is only needed when invoking `aws`, `kubectl`, or `helm` directly — for example during the read-only debugging session below.

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

For the module contract (directory layout, deploy phases, adding a new module) see [`modules.md`](modules.md).

### Deploy a specific module

```bash
just deploy-module arc-staging nodepools
just deploy-module arc-staging arc-runners

# Force a Helm upgrade even when the release looks unchanged
just deploy-module arc-staging arc force
```

### Preview changes without applying

`just plan <cluster>` runs `tofu plan` for the base and every terraform-backed module. Read-only: no apply, no Kubernetes side effects, safe to run from CI on PRs.

```bash
just plan arc-staging
```

### Inspect cluster state

```bash
just show arc-staging        # config, modules, tofu vars
just list                    # all clusters and their modules
```

### Inspect what was deployed

The deploy commands write an audit log into ConfigMaps in the `osdc-system` namespace.

```bash
just deploy-history arc-staging              # list of deploy entries (oldest → newest)
just deploy-status arc-staging               # current versions + recent history (all)
just deploy-status arc-staging arc-runners   # narrow to a single module/command
```

### Post-deploy validation

```bash
just smoke arc-staging              # pytest-based smoke suite (per-module)
just integration-test arc-staging   # full integration suite
just load-test arc-staging          # synthetic load
just workload-test arc-staging      # production workload replay
```

`just deploy` itself prompts to run smoke at the end (skipped when `OSDC_SMOKE=no`).

### Graceful runner refresh

To roll new runner configurations or AMIs without aborting in-flight CI jobs:

```bash
just drain-runners arc-staging      # patch maxRunners=0, taint nodes, wait for in-flight pods
just resume-runners arc-staging     # restore maxRunners from def files, remove the taint
```

`drain-runners` waits up to `OSDC_DRAIN_TIMEOUT_SECS` (default `3600`) for runner pods to finish before declaring stragglers. The default 1h matches typical PyTorch suite duration — raise it for longer test windows.

`taint-nodes` / `untaint-nodes` add or remove the `deploy.osdc.io/refresh-pending=true:NoSchedule` taint on every node labelled `workload-type=github-runner` — useful when you want to mark nodes stale without also draining the AutoscalingRunnerSets.

`recycle-nodes` deletes all Karpenter `NodeClaims`, forcing Karpenter to reprovision on demand. Use after AMI/userData changes when you do not need to preserve in-flight jobs. Staging runs this automatically at the end of `just deploy` (driven by `recycle_karpenter_nodes: true` in `clusters.yaml`).

### Recovering from stale ARC scale sets

Certain `arc-runners` config changes (e.g. changing `github_config_url` from
repo-scoped to org-scoped, or changing `runner_group`) cause the ARC controller to
register new GitHub-side scale sets while old `AutoscalingListener` objects remain
in the cluster pointing at the now-invalid scale set IDs. Symptoms: listener pods
crash-loop with `RunnerScaleSetNotFoundException` (404 from GitHub API), or two
listener pods exist for the same runner set with different spec hashes.

**Step 1 — Check for duplicate or erroring listeners:**

```bash
kubectl get autoscalinglisteners -n arc-systems
kubectl get pods -n arc-systems | grep -v Running
```

If you see two listeners for the same runner set (different hash suffixes), or pods
cycling through `Error` / `ContainerCreating`, proceed with the cleanup.

**Step 2 — Delete all `AutoscalingRunnerSet` objects to force re-registration:**

```bash
kubectl delete autoscalingrunnersets --all -n arc-runners
```

This removes the in-cluster ARS objects. The ARC Helm releases are untouched — the
controller recreates ARS objects (and fresh GitHub scale set IDs) on the next deploy.

**Step 3 — Force-redeploy arc-runners:**

```bash
HELM_FORCE_UPGRADE=1 just deploy-module <cluster> arc-runners
```

`HELM_FORCE_UPGRADE=1` bypasses the skip-if-no-diff logic so the upgrade runs even
when the rendered templates haven't changed.

**Step 4 — Clean up any remaining stale listeners:**

After the redeploy, new listeners get a fresh spec hash. Old listeners (prior hash)
may linger briefly. Delete them explicitly:

```bash
OLD_HASH=<old-hash>   # e.g. 58d9767d — visible in the listener name
kubectl delete autoscalinglistener -n arc-systems \
  $(kubectl get autoscalinglisteners -n arc-systems --no-headers | grep "$OLD_HASH" | awk '{print $1}')
```

All listener pods should reach `Running` within ~30 seconds.

> **Note:** For `runner_group`-only changes (no URL change), you typically do **not**
> need to delete the ARS objects — a `HELM_FORCE_UPGRADE=1` redeploy is sufficient.
> Only stale listeners need manual cleanup in that case.

### Read-only debugging

```bash
kubectl get nodes
kubectl get pods -A
kubectl get nodepools                                # requires the karpenter module
kubectl get autoscalingrunnersets -n arc-runners
helm list -A
```

### Environment variables for `just deploy`

Set these to suppress the interactive prompts in `just deploy` — required for CI/automation use:

| Variable | Values | Default | Effect |
|----------|--------|---------|--------|
| `OSDC_CONFIRM` | `yes` / `no` / `ask` | `ask` | Confirm before applying the full deploy and each tofu plan. `no` cancels immediately. |
| `OSDC_TAINT_NODES` | `yes` / `no` / `ask` | `ask` | Taint ARC runner nodes after the deploy completes (skipped when nodes are being recycled). |
| `OSDC_SMOKE` | `yes` / `no` / `ask` | `ask` | Run `just smoke <cluster>` after the deploy. |
| `OSDC_DRAIN_TIMEOUT_SECS` | seconds | `3600` | How long `just drain-runners` waits for in-flight runner pods before listing stragglers. |

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
