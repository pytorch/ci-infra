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
│   ├── logging/            # Centralized log collection (Alloy DaemonSet → Grafana Cloud Loki)
│   └── node-compactor/     # Node consolidation controller (taints underutilized nodes)
├── modules/                # Optional, per clusters.yaml
│   ├── eks/                # AWS infrastructure (VPC, EKS, Harbor S3/IAM, image mirroring)
│   ├── karpenter/          # Karpenter controller + AWS infra (IAM, SQS, EventBridge)
│   ├── arc/                # ARC controller (GitHub Actions)
│   ├── nodepools/          # Karpenter NodePools (pure compute provisioning)
│   ├── arc-runners/        # ARC runner scale sets (requires arc + nodepools)
│   ├── buildkit/           # BuildKit build service (arm64 + amd64, HAProxy LB)
│   └── monitoring/         # Metrics pipeline: kube-prometheus-stack CRDs/exporters + Alloy → Grafana Cloud
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

## Justfile Kubeconfig Convention (MANDATORY)

**Every just recipe that interacts with a live cluster MUST call `aws eks update-kubeconfig` before doing any kubectl/helm work.** This prevents operators from accidentally running commands against the wrong cluster.

The pattern (in shebang recipes):
```bash
aws eks update-kubeconfig --name "$CNAME" --region "$REGION" --alias "$CNAME" >/dev/null
```

This applies to: `deploy-base`, `deploy-module`, `smoke`, `test-compactor`, `recycle-nodes`, and any future recipe that calls kubectl, helm, or deploy scripts. When adding a new recipe that touches a cluster, always include the kubeconfig step.

## Automation Hierarchy

**Required order of preference for new automation:**

1. **just recipes** — use existing just recipes for all tasks; check justfile first before creating anything
2. **Python scripts** — for new automation requiring logic/complexity
3. **Bash scripts** — ONLY when Python is unsuitable OR trivial (< 20 lines)

**DO NOT create bash scripts if a Python solution is reasonable.** Python provides better error handling, testability, and maintainability.

**ALWAYS use `uv` for Python dependencies** (`uv pip install`, `uv venv`, `uv run`). NEVER use pip, conda, poetry, or other package managers.

## Unit Tests (MANDATORY)

**Every new Python script with testable logic MUST have co-located unit tests.** When adding or modifying functionality in ANY script, you MUST check for existing tests and update them to cover the changes. When adding new functionality, add corresponding tests. No code change is complete without verifying its test coverage.

- **Co-located tests**: Test files live next to the script they test (e.g., `scripts/python/foo.py` → `scripts/python/test_foo.py`)
- **Pure-logic extraction**: Separate testable logic (data transforms, validation, comparisons) from side effects (subprocess calls, kubectl, helm). Test the pure logic; mock the side effects only when necessary.
- **Run before declaring done**: `just test` must pass clean. No skipped tests, no xfails for new code.
- **Coverage expectation**: Test happy paths, edge cases (empty inputs, missing keys), and any cross-module interaction scenarios (e.g., multi-module namespace sharing).

## Smoke Tests (MANDATORY)

Post-deploy validation that a cluster is actually healthy. Run via `just smoke <cluster>` or automatically at the end of `just deploy` (controlled by `OSDC_SMOKE=yes|no|ask`).

Smoke tests verify that what was deployed is actually working: Helm releases are deployed, pods are Running, DaemonSets have all pods ready, required secrets and service accounts exist, AWS resources (IAM roles, SQS queues) are present, and Karpenter NodePools / ARC runner scale sets match their definition files. They do NOT test application-level behavior — just that the deployment landed correctly.

**Architecture**: Each module owns its own smoke tests, co-located at `<module>/tests/smoke/`. A root `tests/smoke/conftest.py` provides shared fixtures (batch-fetched cluster state, cluster config, CLI helpers) that all module tests inherit via pytest's conftest discovery. `just smoke <cluster>` reads `clusters.yaml`, finds enabled modules, collects their `tests/smoke/` directories, and runs pytest across all of them. Adding a module with smoke tests = they're discovered automatically. Moving a module = its tests move with it.

**When changing ANY module or base component, you MUST update its smoke tests to cover the change.** If the component doesn't have smoke tests yet, add them. Smoke tests are the safety net that catches deployment regressions — they only work if they stay current.

- Smoke test files live in `<component>/tests/smoke/test_*.py`
- Shared helpers and retry logic are in `tests/smoke/helpers.py`
- Each module's `tests/smoke/conftest.py` re-exports the root conftest: `from smoke_conftest import *`
- DaemonSet checks use `assert_daemonset_healthy()` which tolerates mismatches caused by nodes in transition (new, NotReady, or being deleted). Deployment checks use `assert_deployment_ready()` with a 90-second retry window for rollout tolerance
- Consumer modules can have their own `tests/smoke/` — they're discovered the same way

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
just recycle-nodes <cluster>           # Terminate Karpenter nodes (reprovisioned on demand)
just test                              # Run all unit tests
just test-compactor <cluster>          # Run node-compactor e2e tests
just smoke <cluster>                   # Run smoke tests against a deployed cluster
just integration-test <cluster>        # Run full integration test against a cluster
just lint                              # Lint all code (shell, Python, Docker, k8s, tf)
just lint-fix                          # Auto-fix lint issues (ruff, shfmt, tofu fmt, taplo)
just analyze-utilization               # Analyze runner-to-node packing efficiency
just simulate-cluster                  # Monte Carlo cluster simulation for CI load
```

### Base deploy order (always)

1. **Terraform** — VPC, EKS, Harbor S3/IAM (parameterized, no per-env dirs)
2. **Mirror images** — Harbor bootstrap images to ECR (Harbor can't cache itself)
3. **Base k8s** — StorageClass, NVIDIA plugin, git-cache (two-tier), performance tuning
4. **Harbor** — Pull-through cache (caches docker.io, ghcr.io, nvcr.io, registry.k8s.io, quay.io)
5. **Logging** — Alloy DaemonSet for centralized log collection → Grafana Cloud Loki (secret-gated)
6. **Node compactor** — Taints underutilized Karpenter nodes for consolidation (if enabled)

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
- **Monitoring is a module, logging is base** — metrics collection (Alloy Deployment → Grafana Cloud Mimir) is opt-in via the `monitoring` module. Log collection (Alloy DaemonSet → Grafana Cloud Loki) is in `base/logging/` because every cluster needs it. Both are secret-gated — no credentials, no Alloy.
- **Two Alloy installations** — monitoring and logging each deploy their own Alloy with separate Helm releases, namespaces, and RBAC. This avoids config/permission conflicts and lets them scale independently (Deployment for metrics, DaemonSet for logs).

## Runner & NodePool Change Checklist (MANDATORY)

When changing runner definitions (`modules/arc-runners/defs/`) or NodePool definitions (`modules/nodepools/defs/`), you MUST update the following `scripts/python/` files to stay in sync:

| File | What to update |
|------|---------------|
| `scripts/python/instance_specs.py` | `INSTANCE_SPECS` (add/update instance types), `ENI_MAX_PODS` (add/update ENI limits) |
| `scripts/python/pytorch_workload_data.py` | `OLD_TO_NEW_LABEL` (update old→new runner name mappings when names change) |
| `scripts/python/simulate_cluster.py` | Uses `analyze_node_utilization` functions — verify simulation still works |
| `scripts/python/simulate_cluster_cli.py` | CLI entry point for simulation — re-run to validate packing |
| `integration-tests/workflows/integration-test.yaml.tpl` | Update `runs-on` labels if runner names changed |
| `docs/runner_naming_convention.md` | Update runner name examples and mapping tables |

**Verification**: After any runner/nodepool change, run `just analyze-utilization` to confirm packing efficiency and `just test` to verify all scripts agree on the new values.

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

## Observability: Monitoring + Logging

OSDC has two observability pipelines, both pushing to Grafana Cloud. They use **two separate Grafana Alloy installations** to avoid RBAC and config collisions:

| Pipeline | Component | Location | Alloy Mode | Namespace | Helm Release |
|----------|-----------|----------|------------|-----------|--------------|
| **Metrics** | `modules/monitoring/` | Module (opt-in) | Deployment (2 replicas, clustered) | `monitoring` | `alloy` |
| **Logs** | `base/logging/` | Base (every cluster) | DaemonSet (one per node) | `logging` | `alloy-logging` |

Both are **secret-gated**: Alloy only installs if a `grafana-cloud-credentials` secret exists in the respective namespace. The secrets have different keys (metrics uses `username`/`password`; logging uses `loki-username`/`loki-api-key-write`/`loki-api-key-read`). The Grafana Cloud URL for metrics comes from `clusters.yaml`, not the secret.

### Monitoring (metrics pipeline)

The `monitoring` module uses kube-prometheus-stack as a **CRD + exporter bundle only** — Prometheus, Grafana, and AlertManager are all disabled. Alloy discovers ServiceMonitor/PodMonitor CRDs, scrapes targets, and pushes to Grafana Cloud Mimir.

What kube-prometheus-stack provides: CRDs (`monitoring.coreos.com`), Prometheus Operator, node-exporter (DaemonSet on every node), kube-state-metrics.

**What's scraped:**

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

**Configuration (clusters.yaml):**

```yaml
monitoring:
  namespace: monitoring
  grafana_cloud_url: "https://prometheus-prod-36-prod-us-west-0.grafana.net/api/prom/push"
```

### Logging (log collection pipeline)

The `base/logging/` component collects pod logs and system journal entries from every node via an Alloy DaemonSet, pushing to Grafana Cloud Loki.

Two log sources: pod logs (`loki.source.file` reading CRI-format `/var/log/pods/`) and system journal (`loki.source.journal` — kubelet, containerd, kernel only). Kubernetes events are NOT collected (no leader-election support in DaemonSet mode).

Per-module log parsing: modules can contribute `stage.match` blocks via `logging/pipeline.alloy` files. The `assemble_config.py` script discovers these and inserts them at the `// MODULE_PIPELINES` marker in the base Alloy config. See `base/logging/CLAUDE.md` for details.

**Configuration (clusters.yaml):**

```yaml
logging:
  namespace: logging
  grafana_cloud_loki_url: "https://logs-prod-021.grafana.net/loki/api/v1/push"
```

### Key gotchas (both pipelines)

- **RBAC isolation**: Logging uses `fullnameOverride: alloy-logging` in Helm values to avoid ClusterRole/ClusterRoleBinding collision with the monitoring Alloy
- **CRD ordering**: ServiceMonitor/PodMonitor CRDs don't exist until kube-prometheus-stack installs. Monitors live in `kubernetes/monitors/` applied by `deploy.sh` after Helm install — not in the main kustomization
- **Admission webhook**: kube-prometheus-stack's admission webhook job has no tolerations by default. On OSDC clusters where all base nodes are tainted, the job stays Pending forever. Helm values must include tolerations for `prometheusOperator.admissionWebhooks.patch`
- **Credential setup**: Each pipeline needs its own secret created manually before first deploy (see component CLAUDE.md files for exact commands)

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
kubectl get pods -n logging                          # Alloy log collector pods
kubectl get ds -n logging                            # Logging DaemonSet (should match node count)
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

### Querying Logs in Grafana Cloud Loki

**When a pod has terminated, a node was evicted, or `kubectl logs` is unavailable**, logs are still available in Grafana Cloud Loki. The Alloy DaemonSet pushes all pod and system journal logs to Loki continuously, so historical logs survive pod/node termination.

**Step 1 — Extract credentials from the cluster:**

```bash
LOKI_USER=$(kubectl get secret grafana-cloud-credentials -n logging -o jsonpath='{.data.loki-username}' | base64 -d)
LOKI_READ_KEY=$(kubectl get secret grafana-cloud-credentials -n logging -o jsonpath='{.data.loki-api-key-read}' | base64 -d)
LOKI_URL="https://logs-prod-021.grafana.net"
```

The Loki URL comes from `clusters.yaml` (`logging.grafana_cloud_loki_url`), minus the `/loki/api/v1/push` suffix.

**Step 2 — Query with curl:**

```bash
# Journal logs by unit (kubelet, containerd)
curl -s -u "$LOKI_USER:$LOKI_READ_KEY" \
    "$LOKI_URL/loki/api/v1/query_range" \
    --data-urlencode 'query={cluster="<cluster-name>", unit="kubelet.service"}' \
    --data-urlencode "limit=100" \
    --data-urlencode "start=$(date -u -v-1H +%s)000000000" \
    --data-urlencode "end=$(date -u +%s)000000000" | jq .

# Filter by node (structured metadata — use pipe syntax)
curl -s -u "$LOKI_USER:$LOKI_READ_KEY" \
    "$LOKI_URL/loki/api/v1/query_range" \
    --data-urlencode 'query={cluster="<cluster-name>", unit="kubelet.service"} | node="<node-name>"' \
    --data-urlencode "limit=100" \
    --data-urlencode "start=$(date -u -v-1H +%s)000000000" \
    --data-urlencode "end=$(date -u +%s)000000000" | jq .

# Pod logs by namespace + container (when pod log collection is active)
curl -s -u "$LOKI_USER:$LOKI_READ_KEY" \
    "$LOKI_URL/loki/api/v1/query_range" \
    --data-urlencode 'query={cluster="<cluster-name>", namespace="arc-runners", container="runner"}' \
    --data-urlencode "limit=100" \
    --data-urlencode "start=$(date -u -v-1H +%s)000000000" \
    --data-urlencode "end=$(date -u +%s)000000000" | jq .

# Filter pod logs by specific pod name (structured metadata)
curl -s -u "$LOKI_USER:$LOKI_READ_KEY" \
    "$LOKI_URL/loki/api/v1/query_range" \
    --data-urlencode 'query={cluster="<cluster-name>", namespace="arc-runners"} | pod="<pod-name>"' \
    --data-urlencode "limit=100" \
    --data-urlencode "start=$(date -u -v-6H +%s)000000000" \
    --data-urlencode "end=$(date -u +%s)000000000" | jq .

# List available labels
curl -s -u "$LOKI_USER:$LOKI_READ_KEY" "$LOKI_URL/loki/api/v1/labels" | jq .

# List values for a label
curl -s -u "$LOKI_USER:$LOKI_READ_KEY" "$LOKI_URL/loki/api/v1/label/cluster/values" | jq .
```

**Available labels:**

| Label | Type | Usage in LogQL |
|-------|------|---------------|
| `cluster` | Indexed | `{cluster="pytorch-arc-staging"}` |
| `namespace` | Indexed | `{namespace="arc-runners"}` |
| `container` | Indexed | `{container="runner"}` |
| `app` | Indexed | `{app="harbor-core"}` |
| `unit` | Indexed | `{unit="kubelet.service"}` (journal logs only) |
| `pod` | Structured metadata | `\| pod="my-pod-xyz"` (pipe syntax) |
| `node` | Structured metadata | `\| node="ip-10-4-34-110..."` (pipe syntax) |

**Note:** `pod` and `node` are structured metadata (Loki 3.x), not indexed labels. Use the pipe `|` syntax to filter on them, not label matchers `{}`. Indexed labels go inside `{}`, structured metadata goes after `|`.

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

## Before Declaring Work Complete (MANDATORY)

Before declaring any code change complete, you MUST run both of these and they MUST pass clean:

```bash
just lint    # All 11 linters must pass with zero errors
just test    # All unit tests must pass
```

If either fails, fix the issues before finishing. Do not defer lint or test failures — they block CI and break other contributors.

## Code Style & Linting

`just lint` runs **11 linters**. `just lint-fix` auto-fixes what it can (ruff, shfmt, tofu fmt, taplo). All must pass — CI blocks on any failure.

### Indentation (most common agent mistake)

| Language | Indent | Tool |
|----------|--------|------|
| Python | 4 spaces | ruff |
| Shell (.sh) | **2 spaces** | shfmt (`-i 2 -ci -bn`) |
| YAML | 2 spaces (sequences indented) | yamllint |
| HCL/Terraform | 2 spaces | tofu fmt |
| TOML | 2 spaces | taplo |
| Dockerfile, JSON, Alloy | 4 spaces | .editorconfig |

### Python (ruff)

- **Line length: 120** (not 80/88)
- **Target: Python 3.12**
- Imports must be sorted (isort rules enabled). `known-first-party = ["osdc"]`
- No commented-out code (`ERA001`) — except in test files
- Use comprehensions over `map()`/`filter()` (`C4`)
- Don't use `assert` outside test files (`S101`)
- `print()` is allowed (`T201` ignored)
- `/tmp` paths are allowed (`S108` ignored)
- `subprocess` calls are allowed (`S603`/`S607` ignored)

### Shell (shellcheck + shfmt)

- **2-space indent**, case bodies indented (`-ci`), binary ops (`&&`/`||`) start the next line (`-bn`)
- Always quote variables (shellcheck `SC2086`)
- Follow shellcheck defaults — no custom config

### YAML (yamllint)

- 2-space indent, sequences indented too
- Truthy values: only `true`, `false`, `yes`, `no` (not `on`/`off`/`True`/`False`)
- Max line length: 200 (warning only)
- No trailing whitespace, newline at EOF, max 2 consecutive blank lines

### Kubernetes (kubeconform + kube-linter)

- All manifests validated against official schemas (strict mode)
- Resource requirements, readiness probes, and other best-practice checks are active
- Many infra-specific checks disabled (privileged containers, hostPath, etc.) — see `.kube-linter.yaml`

### Terraform (tflint + tofu fmt)

- AWS plugin enabled (catches invalid instance types, deprecated resources)
- Canonical formatting enforced by `tofu fmt`

### Dockerfiles (hadolint)

- Standard rules, but apt/pip version pinning not required (`DL3008`/`DL3013` ignored)

### Security (trivy)

- Scans `base/` and `modules/` for HIGH/CRITICAL IaC issues
- Known exceptions in `.trivyignore` (public EKS API, privileged DaemonSets, etc.)

### All files (.editorconfig)

- LF line endings, trailing newline required, no trailing whitespace

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
| `base/logging/deploy.sh` | Secret-gated Alloy DaemonSet install for log collection → Grafana Cloud Loki |
| `base/logging/pipelines/base.alloy` | Base Alloy River config (pod logs, journal, loki.write, MODULE_PIPELINES marker) |
| `base/logging/helm/alloy-logging-values.yaml` | Alloy DaemonSet Helm values (tolerates all taints, journal mount, positions hostPath) |
| `base/logging/scripts/python/assemble_config.py` | Assembles base pipeline + per-module `stage.match` blocks into ConfigMap |

## Documentation (`docs/`)

**Keep this list in sync.** When adding, removing, or significantly editing a document in `docs/`, update this table (description, filename, or entry presence).

| Document | Description |
|----------|-------------|
| `architecture.md` | Modular platform architecture — base vs modules, deployment layers |
| `current_runner_load_distribution.md` | 30-day job counts and peak concurrency analysis by runner type |
| `initialize-containers-slowness.md` | Root cause analysis of slow "Initialize containers" step (ARC hook bottleneck) |
| `loki_query.md` | CLI guide for querying Grafana Cloud Loki logs (credentials, LogQL patterns) |
| `mimir_query.md` | CLI guide for querying Grafana Cloud Mimir metrics (credentials, PromQL patterns) |
| `modules.md` | Module contract and deployment order (Terraform → Kubernetes → deploy.sh) |
| `node-utilization-optimization.md` | Runner-to-node packing efficiency analysis and instance type recommendations |
| `observability.md` | Two-pipeline observability architecture (metrics Deployment + logs DaemonSet → Grafana Cloud) |
| `operations.md` | Operational procedures — bootstrap, deploy, and manage clusters |
| `pr-migration.md` | Migration history from monolithic `arc/` to modular `osdc/` layout |
| `runner_naming_convention.md` | Compact runner label naming scheme (42-char limit, abbreviation rules) |
