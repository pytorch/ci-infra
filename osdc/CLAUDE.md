# CLAUDE.md — OSDC (Open Source Dev Cloud)

project-doc: enabled

## What This Is

Modular Kubernetes infrastructure platform on AWS EKS. A shared `base/` provides the cluster (VPC, EKS, Harbor, git cache, GPU plugins), and optional `modules/` layer services on top (ARC, runners, BuildKit, future projects). One codebase drives multiple clusters across regions via `clusters.yaml`.

Working directory: `osdc/`. Run all commands from here.

## NEVER USE TERRAFORM — USE TOFU ONLY

This project uses **OpenTofu** (`tofu`), NOT Terraform. Running `terraform` commands **will corrupt the state file** and can destroy infrastructure. There is no recovery.

- **NEVER** run `terraform init`, `terraform plan`, `terraform apply`, or any `terraform` subcommand
- **ALWAYS** use `tofu` or `just` recipes (which call `tofu` internally)
- Directories are named `terraform/` but the tool is `tofu`
- In `mise.toml`, the entry is `opentofu`, never `terraform`

## Project Organization — Critical

**Before making ANY changes, understand how this project is organized.** This project MUST be well-organized at all times:

1. **Base vs modules**: Shared infrastructure goes in `base/`. Optional services go in `modules/<name>/`. If every cluster needs it, it's base.
2. **Folder separation is mandatory**: Separate directories for `terraform/`, `kubernetes/`, `helm/`, `scripts/`, `docker/` within each component.
3. **Technology separation**: Don't mix Python, Terraform, Bash, Helm, and Kubernetes manifests in the same directory.
4. **Cloud separation**: Separate cloud-specific code (AWS/Kubernetes) from cloud-agnostic definitions (runner defs).

**Before planning changes:**
- **FIRST**: Examine the current project structure
- **THEN**: Plan changes to maintain or improve the organization
- **NEVER**: Mix unrelated files or technologies in the same directory

```
osdc/
├── clusters.yaml           # THE source of truth — clusters + module lists
├── justfile                # All operations (deploy, lint, test, setup)
├── mise.toml               # Tool versions (tofu, kubectl, helm, crane, ruff, etc.)
├── pyproject.toml          # Python deps + dev deps (managed by uv)
├── scripts/                # Orchestration helpers
│   ├── cluster-config.py   # Reads clusters.yaml, outputs values for just/shell
│   ├── bootstrap-state.sh  # Creates S3 + DynamoDB for tofu state
│   ├── mise-activate.sh    # Sourceable helper for shebang recipes needing mise tools
│   └── python/
│       └── configure_harbor_projects.py  # Harbor proxy cache project setup
├── base/                   # Deployed to EVERY cluster
│   ├── kubernetes/         # StorageClass, NVIDIA plugin, git-cache, Harbor NS, perf tuning
│   │   └── git-cache/      # Two-tier git cache (central Deployment + rsync DaemonSet)
│   ├── helm/               # Harbor values
│   ├── docker/             # Container images (runner-base)
│   ├── scripts/            # Bootstrap (EKS node setup)
│   └── node-compactor/     # Node consolidation controller (taints underutilized nodes)
├── modules/                # Optional, per clusters.yaml
│   ├── eks/                # AWS infrastructure (VPC, EKS, Harbor S3/IAM, image mirroring)
│   ├── karpenter/          # Karpenter controller + AWS infra (IAM, SQS, EventBridge)
│   ├── arc/                # ARC controller (GitHub Actions)
│   ├── nodepools/          # Karpenter NodePools (pure compute provisioning)
│   ├── arc-runners/        # ARC runner scale sets (requires arc + nodepools)
│   ├── buildkit/           # BuildKit build service (arm64 + amd64, HAProxy LB)
│   └── monitoring/         # Prometheus, Grafana, AlertManager, DCGM exporter, Alloy
└── docs/                   # Architecture and operations docs
```

## Using as a Submodule

This OSDC platform can be used standalone (clone, add clusters.yaml, deploy) or as a git submodule in another repo. The submodule pattern allows consumers to maintain their own `clusters.yaml` and custom modules while pulling upstream updates.

### Consumer repo structure

```
your-repo/osdc/
├── upstream/              # git submodule -> pytorch/ci-infra
├── clusters.yaml          # your cluster definitions
├── modules/               # your custom modules (checked before upstream)
│   └── re-devgpu/
├── justfile               # thin wrapper that imports upstream
└── mise.toml              # your tool versions
```

### Consumer justfile

```just
set dotenv-load := true
set shell := ["mise", "exec", "--", "bash", "-euo", "pipefail", "-c"]

ROOT := justfile_directory()
UPSTREAM := ROOT / "upstream" / "osdc"
CLUSTERS_YAML := ROOT / "clusters.yaml"

import 'upstream/osdc/justfile'
```

Key variables: `ROOT` = consumer directory (where `clusters.yaml` lives), `UPSTREAM` = upstream `osdc/` (where `base/`, `modules/`, `scripts/` live). Module resolution checks `ROOT/modules/` first, falls back to `UPSTREAM/modules/`.

### Environment variables

Deploy scripts accept these env vars (set automatically by the justfile):
- `OSDC_ROOT` — consumer's osdc/ directory
- `OSDC_UPSTREAM` — upstream's osdc/ directory
- `CLUSTERS_YAML` — path to clusters.yaml

When running standalone, all three default to the local directory.

## Key Tools

- **OpenTofu** (`tofu`): Infrastructure as code. See warning above — never use `terraform`.
- **just**: Command runner. Recipes in `osdc/justfile`. Single entry point for all operations.
- **mise**: Tool version manager. Config in `osdc/mise.toml`. Auto-installs tools on first run.
- **crane**: Container image mirroring tool (go-containerregistry CLI).
- **uv**: Python package manager. Always use `uv`, never pip/conda/poetry.

## Justfile + Mise Gotcha

The justfile uses `set shell := ["mise", "exec", "--", "bash", "-euo", "pipefail", "-c"]` so that mise-managed tools (tofu, crane, kubectl, etc.) are on PATH.

**Shebang recipes bypass this.** A recipe starting with `#!/usr/bin/env bash` is executed as a standalone script by just, NOT through the configured shell. This means mise-managed tools may not be on PATH inside shebang recipes or scripts they call.

For recipes that need mise tools, use non-shebang style (line-by-line with `@` prefix) so they run through `mise exec`. If a shebang recipe is required, call subscripts via `mise exec -- ./script.sh` explicitly.

## Automation Hierarchy

**Required order of preference for new automation:**

1. **just recipes** — use existing just recipes for all tasks; check justfile first before creating anything
2. **Python scripts** — for new automation requiring logic/complexity
3. **Bash scripts** — ONLY when Python is unsuitable OR trivial (< 20 lines)

**DO NOT create bash scripts if a Python solution is reasonable.** Python provides better error handling, testability, and maintainability.

**ALWAYS use `uv` for Python dependencies** (`uv pip install`, `uv venv`, `uv run`). NEVER use pip, conda, poetry, or other package managers.

## How Deployment Works

`clusters.yaml` defines every cluster and its modules. The justfile reads it via `scripts/cluster-config.py`.

```bash
just setup                             # Install mise tools + Python venv
just list                              # Show all clusters and modules
just show <cluster>                    # Inspect cluster config
just kubeconfig <cluster>              # Configure kubectl for a cluster
just bootstrap <cluster>               # Create S3 state bucket + DynamoDB
just bootstrap-all                     # Bootstrap all clusters
just deploy <cluster>                  # Full deploy (base + modules)
just deploy-base <cluster>             # Base only
just deploy-module <cluster> <module>  # Single module
just test                              # Run all unit tests
just test-compactor <cluster>          # Run node-compactor e2e tests
just lint                              # Lint all code (shell, Python, Docker, k8s, tf)
```

### Base deploy order (always)

1. **Terraform** — VPC, EKS, Harbor S3/IAM (parameterized, no per-env dirs)
2. **Mirror images** — Harbor bootstrap images to ECR (Harbor can't cache itself)
3. **Harbor** — Pull-through cache (first k8s workload; caches docker.io, ghcr.io, nvcr.io, registry.k8s.io, quay.io)
4. **Base k8s** — StorageClass, NVIDIA plugin, git-cache (two-tier), performance tuning
5. **Node compactor** — Taints underutilized Karpenter nodes for consolidation (if enabled)

### Module deploy order (per clusters.yaml list order)

Each module may have:

| File | Purpose | Applied by justfile |
|------|---------|-------------------|
| `terraform/main.tf` | AWS resources (optional) | `tofu apply` with cluster vars |
| `kubernetes/kustomization.yaml` | K8s manifests (optional) | `kubectl apply -k` |
| `deploy.sh` | Custom deploy logic (optional) | Called with `(cluster-id, cluster-name, region)` |

The justfile auto-detects which of these exist and runs them in order: terraform → kubernetes → deploy.sh.

### clusters.yaml drives everything

There is no hardcoded staging/production concept. Each cluster is an independent installation with its own config. `clusters.yaml` controls everything: infrastructure sizing, component tuning (Harbor replicas, Karpenter PDB, ARC log level), module-specific settings (BuildKit replicas, runner GitHub config, max runner counts), and module list.

Adding a new cluster: add an entry to `clusters.yaml` with region, cluster_name, state_bucket, base config, component tuning, module config, and module list. Then `just bootstrap <cluster-id>` + `just deploy <cluster-id>`.

Adding a module to a cluster: append the module name to the cluster's `modules` list.

To create a new module: make a directory under `modules/`, add any of the files above, and list the module name in `clusters.yaml` for the target cluster.

Reading cluster config in scripts: `uv run scripts/cluster-config.py <cluster-id> <dot.path> [default]`. Supports nested paths (e.g., `harbor.core_replicas`). Falls back to `defaults:` section, then optional third argument.

## Terraform Architecture

Single parameterized root at `modules/eks/terraform/`. No per-environment directories.

- Variables (`cluster_name`, `aws_region`, `vpc_cidr`, etc.) come from `clusters.yaml` → justfile → `tofu -var=`
- Backend configured at `tofu init` via `-backend-config` (bucket, key, region)
- Each cluster gets its own state: `s3://<state_bucket>/<cluster-id>/base/terraform.tfstate`
- Modules with their own terraform get: `s3://<state_bucket>/<cluster-id>/<module>/terraform.tfstate`. Module terraform also receives `state_bucket` and `cluster_id` as vars.

## Key Design Decisions

- **Harbor is always-on** — baked into base, not a module. Every cluster needs a pull-through cache.
- **Git cache + NVIDIA plugin in base** — foundational infrastructure, not optional.
- **Karpenter is a module** — allows clusters to opt-in to Karpenter. The controller, IAM, SQS, and EventBridge are all in `modules/karpenter/`. NodePools are separate in `modules/nodepools/`.
- **Compute split: nodepools + arc-runners** — `nodepools` deploys pure Karpenter NodePools (compute provisioning). `arc-runners` deploys ARC runner scale sets (requires both `arc` and `nodepools`). Non-ARC clusters can use `nodepools` alone for compute.
- **One terraform root, many clusters** — no code duplication per environment.
- **Modules are independent** — each has its own terraform state, k8s resources, deploy script. No cross-module imports.
- **Monitoring is a module** — not baked into base. Clusters opt-in by listing `monitoring` in their modules. In-cluster Prometheus provides local storage and Grafana dashboards; Grafana Cloud push via Alloy is optional and secret-gated.

## EKS Node Taints

Base nodes: `CriticalAddonsOnly=true:NoSchedule`. All base workloads (Harbor, DaemonSets, Karpenter, control plane) must tolerate this.

GPU nodes: `nvidia.com/gpu` + `instance-type` taints.
BuildKit nodes: `instance-type` taints (instance type varies per cluster, see `clusters.yaml` buildkit config).

Always verify tolerations match the target nodes' taints when adding new workloads.

## Harbor Helm Chart Gotchas

The Harbor chart (v1.18.2) has inconsistent value paths across components:

- **No `global.imageRegistry`**: Each component's image must be overridden individually via `--set core.image.repository=...`, `--set registry.registry.image.repository=...`, etc.
- **Nested vs top-level values**: Most components use top-level paths (`core.tolerations`, `registry.tolerations`), but `redis` and `database` require values under `.internal` (`redis.internal.tolerations`, `database.internal.tolerations`). Likewise images: `redis.internal.image.repository`, `database.internal.image.repository`, `registry.registry.image.repository`, `registry.controller.image.repository`.
- **All base-infrastructure nodes are tainted** with `CriticalAddonsOnly=true:NoSchedule`. Every Harbor component (including nginx, exporter) must have matching tolerations or pods will be unschedulable.

## Image Mirroring

`modules/eks/images.yaml` contains **only bootstrap images** — the Harbor components that must be mirrored to ECR because Harbor cannot cache its own images. Once Harbor is running, all other images (ghcr.io, nvcr.io, docker.io, registry.k8s.io, quay.io) are pulled through Harbor's proxy cache. Containerd on every node is configured to route pulls through Harbor (see `base/scripts/bootstrap/eks-base-bootstrap.sh`). Images from `public.ecr.aws` are NOT mirrored (no rate limits on AWS).

## GitHub Actions Workflow Constraints

All self-hosted runner pods run with `ACTIONS_RUNNER_REQUIRE_JOB_CONTAINER=true`. Every workflow job **must** specify a `container:` image — containerless jobs are rejected at startup. Use `ghcr.io/actions/actions-runner:latest` as the default.

## BuildKit Build Service

BuildKit (`moby/buildkit:v0.27.1`) runs as two Deployments in the `buildkit` namespace — one per architecture. Runner job pods invoke `buildctl` — no Kubernetes API access required.

- **Architecture**: Dual-arch fleet with per-arch Deployments and Services
  - `buildkitd-arm64` — Graviton (default: m8gd.24xlarge), Service: `buildkitd-arm64.buildkit:1234`
  - `buildkitd-amd64` — Intel (default: m6id.24xlarge), Service: `buildkitd-amd64.buildkit:1234`
  - `buildkitd` — combined Service (round-robin across both arches, for arch-agnostic builds)
- **Sizing**: Dynamically computed by `modules/buildkit/scripts/python/generate_buildkit.py` from instance specs. Guaranteed QoS (requests == limits), static CPU pinning, `max-parallelism=1` (one build at a time per pod), 2 pods per node
- **Instance types**: Configurable via `clusters.yaml` (`buildkit.arm64_instance_type`, `buildkit.amd64_instance_type`)
- **Scaling**: Configurable via `clusters.yaml` (`buildkit.replicas_per_arch`, default 4)
- **Storage**: NVMe instance storage (RAID0) for build cache + git object cache
- **Registry mirrors**: `buildkitd.toml` routes `FROM` image pulls through Harbor (same as containerd on runner nodes)
- **Network access**: NetworkPolicy restricts ingress to pods from `arc-runners` namespace only
- **Load balancing**: HAProxy (least-connections) distributes `buildctl` connections across buildkitd pods per architecture

### Targeting an architecture

```bash
# Build an ARM64 image
buildctl --addr tcp://buildkitd-arm64.buildkit:1234 build --output type=image,name=$IMAGE,push=true ...

# Build an x86_64 image
buildctl --addr tcp://buildkitd-amd64.buildkit:1234 build --output type=image,name=$IMAGE,push=true ...

# Multi-arch: build both, then combine with crane
crane index append -t $IMAGE -m $IMAGE-arm64 -m $IMAGE-amd64
```

### Using the git cache in Dockerfiles

The git-cache rsync DaemonSet runs on BuildKit nodes (same as runner nodes). The buildkitd pod mounts the cache at `/opt/git-cache`. To use it inside a `RUN` step, pass it as a named build context:

```bash
buildctl --addr tcp://buildkitd-arm64.buildkit:1234 build \
  --frontend dockerfile.v0 \
  --local context=. \
  --local dockerfile=. \
  --opt context:gitcache=local:gitcache \
  --local gitcache=/opt/git-cache \
  --output type=image,name=$IMAGE,push=true
```

Then in the Dockerfile, bind-mount the cache and set the env var:

```dockerfile
RUN --mount=type=bind,from=gitcache,source=pytorch/pytorch.git/objects,target=/tmp/git-objects \
    GIT_ALTERNATE_OBJECT_DIRECTORIES=/tmp/git-objects \
    git clone https://github.com/pytorch/pytorch /workspace
```

## Monitoring (kube-prometheus-stack + Grafana Alloy)

The `monitoring` module deploys a two-tier metrics pipeline. Helm charts used:

- **[kube-prometheus-stack](https://github.com/prometheus-community/helm-charts/tree/main/charts/kube-prometheus-stack)** v82.10.3 — bundles Prometheus, Grafana, AlertManager, Prometheus Operator, node-exporter, and kube-state-metrics.
- **[Grafana Alloy](https://github.com/grafana/alloy)** (Helm chart `grafana/alloy`) — optional push agent for Grafana Cloud. Only installed when a `grafana-cloud-credentials` secret exists in the monitoring namespace.
- **[DCGM Exporter](https://github.com/NVIDIA/dcgm-exporter)** (`nvcr.io/nvidia/k8s/dcgm-exporter:4.5.2-4.8.1-distroless`) — DaemonSet for GPU metrics, runs only on GPU nodes.

### Architecture

**Tier 1 — In-cluster Prometheus**: HA pair (2 replicas) on base infrastructure nodes with gp3 EBS PVCs. Auto-discovers all ServiceMonitors/PodMonitors across all namespaces. Grafana (optional, enabled by default) auto-loads dashboards from ConfigMaps labeled `grafana_dashboard: "1"`. AlertManager (optional, enabled by default) runs as HA pair.

**Tier 2 — Grafana Cloud push via Alloy** (conditional): Alloy independently discovers ServiceMonitor/PodMonitor CRDs, scrapes the same targets as Prometheus, and pushes via `prometheus.remote_write` to Grafana Cloud Mimir. Clustering enabled for HA target distribution across 2 replicas.

### What's scraped

| Type | Name | Target Namespace | What it monitors |
|------|------|-----------------|-----------------|
| ServiceMonitor | arc-controller | arc-systems | ARC controller metrics |
| ServiceMonitor | harbor | harbor-system | Harbor exporter metrics |
| ServiceMonitor | karpenter | karpenter | Karpenter controller metrics |
| ServiceMonitor | node-compactor | kube-system | Node compactor metrics |
| ServiceMonitor | git-cache-central | kube-system | Git cache central pod metrics |
| ServiceMonitor | dcgm-exporter | monitoring | NVIDIA GPU metrics (DCGM) |
| PodMonitor | git-cache-daemonset | kube-system | Git cache DaemonSet metrics |
| PodMonitor | arc-listeners | arc-runners | ARC listener pods metrics |

node-exporter (from kube-prometheus-stack) runs on every node — it tolerates ALL taints. kube-state-metrics runs on base nodes.

### Configuration (clusters.yaml)

```yaml
monitoring:
  namespace: monitoring           # Kubernetes namespace
  retention_days: 15              # Prometheus data retention
  storage_size: 50Gi              # PVC size per Prometheus replica
  grafana_enabled: true           # Deploy Grafana
  alertmanager_enabled: true      # Deploy AlertManager
  grafana_cloud_url: "https://prometheus-prod-36-prod-us-west-0.grafana.net/api/prom/push"
```

### Deploy order (deploy.sh)

1. Create `monitoring` namespace (via kustomization)
2. `helm upgrade --install kube-prometheus-stack` — installs CRDs + all components
3. `kubectl apply -k kubernetes/monitors/` — ServiceMonitors/PodMonitors (depends on CRDs from step 2)
4. Conditionally: `helm upgrade --install alloy` — if `grafana-cloud-credentials` secret exists

### CRD ordering gotcha

The justfile applies `kubernetes/kustomization.yaml` (Phase 2) before running `deploy.sh` (Phase 3). ServiceMonitor/PodMonitor CRDs don't exist until kube-prometheus-stack installs in step 2 of deploy.sh. That's why monitors live in a separate `kubernetes/monitors/` kustomization applied by deploy.sh after Helm install — not in the main kustomization.

### Admission webhook gotcha

The kube-prometheus-stack Helm chart's admission webhook pre-install job has no tolerations by default. On OSDC clusters where ALL base nodes are tainted with `CriticalAddonsOnly`, the job stays Pending forever. The Helm values must include tolerations + nodeSelector for `prometheusOperator.admissionWebhooks.patch`. If the job gets stuck, you must delete the failed Helm release and the stuck job before retrying.

## Git Clone Cache

A two-tier caching system speeds up git clones of large repositories on runner and BuildKit nodes.

**Architecture**:
- **Central pod** — A Deployment with an EBS PVC clones repositories from GitHub. Uses dual-slot rotation (cache-a / cache-b) so DaemonSet clients always read from a consistent snapshot. Serves repos via rsyncd on port 873.
- **DaemonSet** — Runs on every runner/BuildKit node. Periodically rsyncs from the central pod to local NVMe storage at `/opt/git-cache`. Runner pods mount this path read-only.

**How runners use it**: Runner pods get `CHECKOUT_GIT_CACHE_DIR=/opt/git-cache` and `GIT_CONFIG_SYSTEM` (with `safe.directory=*`). Workflow steps that use `actions/checkout` pass `reference-repository: $CHECKOUT_GIT_CACHE_DIR/<repo>` to avoid full clones.

### Key files

| File | What to change |
|------|---------------|
| `base/kubernetes/git-cache/central-configmap.yaml` | `central.py` script — repo list is hardcoded in the Python `REPOS` list |
| `base/kubernetes/git-cache/central-deployment.yaml` | Central pod spec (EBS PVC, rsyncd sidecar) |
| `base/kubernetes/git-cache/daemonset.yaml` | Rsync DaemonSet (syncs from central to NVMe) |
| `modules/arc-runners/templates/runner.yaml.tpl` | `CHECKOUT_GIT_CACHE_DIR` and `GIT_CONFIG_SYSTEM` env vars |

### Adding a new cached repository

1. Edit the `REPOS` list in the `central.py` script inside `base/kubernetes/git-cache/central-configmap.yaml`
2. Redeploy: `just deploy-base <cluster>`

## External Knowledge Base

The `actions-knowledge-base/` directory contains source code, documentation, and detailed reference material for many of the open-source projects and tools used by this project — including Actions Runner Controller, Harbor, the Harbor Helm chart, the GitHub Actions runner, runner images, cloud-provider credential actions, and more. Each project lives as a git submodule under `repos/` and the knowledge base's own `AGENTS.md` indexes every included repository with summaries and key paths.

**Finding it**: The directory is named `actions-knowledge-base` and lives somewhere above or beside this project. Walk upward from the current working directory, checking each ancestor and its children, until you find a directory named `actions-knowledge-base`. It is typically a sibling of the top-level repo (e.g., beside `ciforge/`), but the exact location depends on the checkout layout. Do not hardcode a relative path — search for it.

The knowledge base has two key directories:
- **`actions-knowledge-base/repos/`** — Read-only upstream source code and docs (git submodules). Managed via `sync.py`.
- **`actions-knowledge-base/docs/`** — Our own findings, workarounds, and gotchas discovered during development and operations (e.g., BuildKit OTEL crash, Grafana Alloy setup, CRD ordering issues, deploy phase ordering).

**You should consult this knowledge base** whenever you need to:
- Understand how a dependency works (e.g., ARC Helm chart values, Harbor configuration, runner lifecycle)
- Clarify configuration quirks, edge cases, or undocumented behavior
- Look up default values, API surfaces, or internal implementation details
- Verify correct usage of upstream Helm charts, container images, or action inputs
- Review operational learnings and previously discovered issues (`docs/`)

In most cases, reading the relevant source or docs in the knowledge base first will produce more accurate results than guessing or relying on general knowledge alone. Treat it as a first-class resource.

## Read-Only CLI Debugging (Encouraged)

**You are encouraged to run read-only CLI commands to debug, investigate, and understand cluster and infrastructure state.** This is the fastest way to diagnose issues — use it proactively. All tools below are managed by mise via `osdc/mise.toml`.

Run commands from the `osdc/` working directory so mise activates the correct tool versions. If running from elsewhere, prefix with `mise exec --`:

```bash
mise exec -- kubectl get pods -n arc-runners
```

### kubectl (Kubernetes)

```bash
kubectl get nodes                                    # List nodes and status
kubectl get pods -A                                  # All pods across namespaces
kubectl get pods -n arc-runners                      # Runner pods
kubectl get pods -n arc-systems                      # ARC controller pods
kubectl get pods -n karpenter                        # Karpenter pods
kubectl get pods -n harbor-system                    # Harbor pods
kubectl get pods -n buildkit                         # BuildKit builder pods
kubectl get nodepools                                # Karpenter NodePools
kubectl get autoscalingrunnersets -n arc-runners      # ARC runner scale sets
kubectl describe pod <pod> -n <ns>                   # Pod details and events
kubectl logs <pod> -n <ns>                           # Pod logs
kubectl get events -n <ns> --sort-by=.lastTimestamp  # Recent events
kubectl top nodes                                    # Node resource usage
kubectl top pods -n <ns>                             # Pod resource usage
```

### aws (AWS CLI)

```bash
aws eks describe-cluster --name <cluster-name> --region <region>
aws ec2 describe-instances --filters "Name=tag:eks:cluster-name,Values=<cluster-name>" --query 'Reservations[].Instances[].{ID:InstanceId,Type:InstanceType,State:State.Name}' --output table
aws ecr describe-repositories --region <region>
aws autoscaling describe-auto-scaling-groups --query 'AutoScalingGroups[].{Name:AutoScalingGroupName,Desired:DesiredCapacity,Min:MinSize,Max:MaxSize}' --output table
```

### helm

```bash
helm list -A                                         # All installed releases
helm status <release> -n <ns>                        # Release status
helm get values <release> -n <ns>                    # Current values
helm history <release> -n <ns>                       # Release history
```

### tofu (OpenTofu) — read-only

To inspect state for a specific cluster, first init with the cluster's backend config:

```bash
cd modules/eks/terraform
tofu init -reconfigure \
    -backend-config="bucket=ciforge-tfstate-arc-staging" \
    -backend-config="key=arc-staging/base/terraform.tfstate" \
    -backend-config="region=us-west-2" \
    -backend-config="dynamodb_table=ciforge-terraform-locks"
tofu show                    # Current state
tofu output                  # Output values
tofu state list              # All managed resources
```

## Don't Do

- **NEVER run `terraform`** — use `tofu` or `just` recipes (terraform will corrupt state)
- Don't run state-changing CLI commands directly (apply, delete, install, destroy) — use `just` recipes
- Don't create bash scripts without considering Python first
- Don't use pip/conda/poetry — use `uv` for Python packages
- Don't install packages or run setup scripts without checking first
- Don't update ANY versions (tools, deps, images) without explicit approval
- Don't create documentation files unless explicitly requested
- Don't experiment with the cluster — read-only investigation is fine, but don't change anything
- Don't mix unrelated files or technologies in the same directory

## Key Files

| File | What it does |
|------|-------------|
| `clusters.yaml` | Defines all clusters, modules, and per-installation config (replicas, log levels, runner limits, etc.) |
| `justfile` | All operations (deploy, lint, test, setup, kubeconfig) |
| `mise.toml` | Tool versions (tofu, kubectl, helm, crane, ruff, shellcheck, etc.) + env vars |
| `pyproject.toml` | Python dependencies + dev deps, managed by uv |
| `scripts/cluster-config.py` | Reads clusters.yaml for justfile/shell consumption |
| `scripts/bootstrap-state.sh` | Creates S3 bucket + DynamoDB for tofu state |
| `scripts/mise-activate.sh` | Sourceable helper — adds mise tools to PATH for shebang recipes |
| `scripts/python/configure_harbor_projects.py` | Configures Harbor proxy cache projects via API |
| `modules/eks/terraform/main.tf` | Parameterized infra (VPC, EKS, Harbor) |
| `modules/eks/terraform/variables.tf` | All variables driven from clusters.yaml |
| `modules/eks/images.yaml` | Harbor bootstrap images to mirror to ECR |
| `base/kubernetes/git-cache/` | Two-tier git cache (central Deployment + rsync DaemonSet) |
| `base/node-compactor/` | Node consolidation controller (taints underutilized Karpenter nodes) |
| `modules/karpenter/deploy.sh` | Karpenter controller + AWS infra (IAM, SQS, Helm install) |
| `modules/nodepools/defs/*.yaml` | NodePool definitions (instance type, arch, disk, gpu flag) |
| `modules/nodepools/deploy.sh` | Generate + apply Karpenter NodePools |
| `modules/arc-runners/defs/*.yaml` | Runner definitions (instance type, CPU, memory, GPU, max runners) |
| `modules/arc-runners/deploy.sh` | Generate + install ARC runner scale sets (requires arc module) |
| `modules/buildkit/deploy.sh` | Generate + deploy BuildKit (Deployments, HAProxy, NodePools) |
| `modules/monitoring/deploy.sh` | Deploy kube-prometheus-stack + monitors + conditionally Alloy |
| `modules/monitoring/helm/values.yaml` | kube-prometheus-stack Helm values (node placement, storage, auto-discovery) |
| `modules/monitoring/helm/alloy-values.yaml` | Grafana Alloy Helm values (ServiceMonitor/PodMonitor discovery, remote_write) |
| `modules/monitoring/kubernetes/monitors/` | ServiceMonitors + PodMonitors for OSDC components (ARC, Harbor, Karpenter, etc.) |
| `modules/monitoring/kubernetes/dcgm-exporter/` | DCGM GPU exporter DaemonSet + headless Service (GPU nodes only) |
