# Modules

## What is a module

A self-contained service that layers on top of a base cluster. Modules live under `modules/<name>/` and are enabled per-cluster in `clusters.yaml`.

## Module contract

The justfile auto-detects and runs these in order:

| Phase | File | When |
|-------|------|------|
| 1. Terraform | `terraform/main.tf` | If file exists — `tofu apply` with independent state |
| 2. Kubernetes | `kubernetes/kustomization.yaml` | If file exists — `kubectl apply -k` (note: a `kubernetes/` directory alone is NOT enough — the gate is the `kustomization.yaml` file; modules without one apply manifests imperatively from `deploy.sh`) |
| 3. Custom | `deploy.sh` | If file exists and is executable — called with args |

`deploy.sh` receives three positional arguments:
```bash
$1 = cluster-id      # e.g. "meta-staging-aws-uw1"
$2 = cluster-name    # e.g. "meta-staging-aws-uw1"
$3 = region           # e.g. "us-west-1"
```

A module can use any combination of these — none of them is strictly required individually. The minimum is whatever produces a working phase: a kubernetes-only module needs only `kubernetes/kustomization.yaml` (e.g. `cache-enforcer`); a script-only module needs only `deploy.sh`; a module with all three runs all three phases in order.

The `deploy-module` recipe also exports three environment variables that every real module relies on:
```bash
OSDC_ROOT       # the consumer repo root (often equal to OSDC_UPSTREAM)
OSDC_UPSTREAM   # the upstream osdc/ checkout providing shared scripts and modules
CLUSTERS_YAML   # path to the active clusters.yaml
```
Modules typically open with this boilerplate so they work both via `just deploy-module` and when invoked directly:
```bash
MODULE_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="${OSDC_ROOT:-$(cd "$MODULE_DIR/../.." && pwd)}"
UPSTREAM_ROOT="${OSDC_UPSTREAM:-$REPO_ROOT}"
```

**Module resolution (consumer override).** Before running the three phases, `deploy-module` resolves the module directory as: if `$OSDC_ROOT/modules/<name>/` exists, use it; otherwise fall back to `$OSDC_UPSTREAM/modules/<name>/`. This is how a downstream consumer repo replaces a shared upstream module with a local variant — drop a module of the same name under your own `modules/` and it wins.

**Audit logging.** `deploy-module` wraps each invocation with `scripts/deploy-log.sh` calls (`deploy_log_start` / `deploy_log_finish`) at both `cmd` and `module` scope. Successes and failures are recorded and surfaced by `just deploy-history` and `just deploy-status`.

**`force` argument.** `deploy-module` accepts an optional third positional `force` argument; setting it to any non-empty value (e.g. `just deploy-module my-cluster monitoring force`) exports `HELM_FORCE_UPGRADE=1` for the duration of the run, which the shared `helm-upgrade.sh` helper honours to force a Helm upgrade even when no diff is detected.

## Creating a new module

1. Create directory:
   ```bash
   mkdir -p modules/mymodule
   ```

2. Add a deploy script (minimum):
   ```bash
   # modules/mymodule/deploy.sh
   #!/usr/bin/env bash
   set -euo pipefail
   CLUSTER="$1" CNAME="$2" REGION="$3"
   echo "Deploying mymodule to $CNAME in $REGION"
   # your logic here
   ```
   ```bash
   chmod +x modules/mymodule/deploy.sh
   ```

3. Enable it for a cluster in `clusters.yaml`:
   ```yaml
   clusters:
     my-cluster:
       modules:
         - mymodule
   ```

4. Deploy:
   ```bash
   just deploy-module my-cluster mymodule
   ```

## Adding terraform to a module

If your module needs AWS resources (IAM roles, S3 buckets, etc.):

```
modules/mymodule/
└── terraform/
    ├── main.tf
    ├── variables.tf
    └── outputs.tf
```

The justfile automatically:
- Inits with backend: `s3://<state_bucket>/<cluster-id>/mymodule/terraform.tfstate`
- Passes four `-var=` flags on every `tofu plan`: `cluster_name`, `aws_region`, `state_bucket`, `cluster_id`

Your `variables.tf` MUST declare all four (tofu rejects unknown `-var` flags):
```hcl
variable "cluster_name" { type = string }
variable "aws_region"   { type = string }
variable "state_bucket" { type = string }
variable "cluster_id"   { type = string }
```

Add more variables as needed. If they need cluster-specific values, extend `clusters.yaml` with module-specific config — `scripts/cluster-config.py` is generic (it walks dotted paths against the YAML with per-path fallback: the cluster's own value wins, otherwise the same path is looked up under `defaults`), so no code change is required to read new keys.

## Adding kubernetes resources to a module

```
modules/mymodule/
└── kubernetes/
    ├── kustomization.yaml
    ├── namespace.yaml
    └── ...
```

Applied via `kubectl apply -k`. Create your own namespaces — don't assume base creates them for you.

## Accessing base terraform outputs

If your `deploy.sh` needs values from the base terraform (e.g. VPC ID, OIDC provider ARN), use `UPSTREAM_ROOT` (not `OSDC_ROOT`) so the lookup works in consumer-overridden setups where `OSDC_ROOT` and `OSDC_UPSTREAM` differ:

```bash
MODULE_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="${OSDC_ROOT:-$(cd "$MODULE_DIR/../.." && pwd)}"
UPSTREAM_ROOT="${OSDC_UPSTREAM:-$REPO_ROOT}"
CFG="$UPSTREAM_ROOT/scripts/cluster-config.py"
BUCKET=$(uv run "$CFG" "$CLUSTER" state_bucket)

cd "$UPSTREAM_ROOT/modules/eks/terraform"
tofu init -reconfigure \
    -backend-config="bucket=${BUCKET}" \
    -backend-config="key=${CLUSTER}/base/terraform.tfstate" \
    -backend-config="region=us-west-2" \
    -backend-config="dynamodb_table=ciforge-terraform-locks" \
    >/dev/null 2>&1
VPC_ID=$(tofu output -raw vpc_id)
```

## Checking if another module is enabled

Useful when your module's behavior depends on whether another module is present (like `arc-runners` requiring `arc`):

```bash
CFG="$UPSTREAM_ROOT/scripts/cluster-config.py"

if uv run "$CFG" "$CLUSTER" has-module arc; then
    echo "ARC is enabled, deploying runner scale sets"
fi
```

## Per-module logging pipeline

Modules that emit logs needing custom Loki labels drop a `logging/pipeline.alloy` file in their directory:

```
modules/mymodule/
└── logging/
    └── pipeline.alloy
```

The `logging` module's `assemble_config.py` discovers these via `--modules-dir` / `--upstream-modules-dir` and merges them into the Alloy DaemonSet config. Without this file the module's logs still ship to Loki but get only the default labels. Real examples: `arc/`, `buildkit/`, `karpenter/`, `monitoring/` each have their own `logging/pipeline.alloy`.

## Tests

Most non-delegate modules have a `tests/` directory; pytest tests are auto-collected by `just test`. Exceptions: the four delegate modules (`arc-runners-h100`, `arc-runners-b200`, `nodepools-h100`, `nodepools-b200`) have no `tests/` of their own, and `zombie-cleanup` keeps its tests alongside the script under `scripts/python/test_*.py`. Modules that own deployable behaviour can additionally provide a `tests/smoke/` directory — `just smoke <cluster>` walks every enabled module and runs whichever `tests/smoke` it finds. Every non-delegate module currently has one.

## Delegate modules (GPU variants)

GPU SKUs (`arc-runners-h100`, `arc-runners-b200`, `nodepools-h100`, `nodepools-b200`) are pure delegate modules: they ship only their own `defs/` (and `generated/`), optionally a `scripts/` directory (the `nodepools-*` delegates carry a per-SKU node-setup script), and a 13-line `deploy.sh` that `exec`s the parent module's deploy script after exporting a few env vars:

```bash
#!/usr/bin/env bash
set -euo pipefail
MODULE_DIR="$(cd "$(dirname "$0")" && pwd)"
UPSTREAM_ROOT="${OSDC_UPSTREAM:-$(cd "$MODULE_DIR/../.." && pwd)}"

export ARC_RUNNERS_DEFS_DIR="$MODULE_DIR/defs"
export ARC_RUNNERS_OUTPUT_DIR="$MODULE_DIR/generated"
export ARC_RUNNERS_MODULE_NAME="arc-runners-h100"
exec "$UPSTREAM_ROOT/modules/arc-runners/deploy.sh" "$@"
```

The parent `arc-runners/deploy.sh` and `nodepools/deploy.sh` honour these `*_DEFS_DIR` / `*_OUTPUT_DIR` / `*_MODULE_NAME` overrides. This is how new GPU SKUs are onboarded without forking the parent module.

## Existing modules

### eks

Base AWS infrastructure. Always deployed as part of `deploy-base`, not as an optional module — and unlike the other modules it has **no `deploy.sh`**.

- **terraform/**: VPC (public/private subnets, NAT gateways), EKS cluster (OIDC, addons), Harbor S3 bucket + IAM
- **images.yaml**: Bootstrap images mirrored to ECR (Harbor components that can't cache themselves)
- **scripts/mirror-images.sh**: Image mirroring via `crane`, invoked separately by the justfile (not via the module deploy contract)

### arc

ARC controller for GitHub Actions self-hosted runners.

- **kubernetes/**: `arc-systems` + `arc-runners` namespaces, runner ServiceAccount, LimitRange, hooks ConfigMap, priority classes, capacity-monitor RBAC, `hooks-warmer` DaemonSet (downloads patched runner-container-hooks to host NVMe on the `c7i-runner` fleet). Registry mirror config lives in `base/kubernetes/` (all clusters need it, not just ARC).
- **helm/arc/**: ARC controller Helm values
- **deploy.sh**: `helm upgrade --install` for the ARC controller chart

### karpenter

Karpenter autoscaler controller and supporting AWS infrastructure.

- **terraform/**: IAM roles, SQS interruption queue, EventBridge rules for spot/rebalance/health events
- **deploy.sh**: `tofu apply` for AWS resources, `helm upgrade --install` for Karpenter chart, configures node class defaults

### nodepools

Pure Karpenter NodePool compute provisioning. No ARC, no runner scale sets — just NodePools.

- **defs/**: NodePool definitions (source of truth) — one per instance type (name, type, arch, disk, gpu flag)
- **generated/**: Auto-generated Karpenter NodePool + EC2NodeClass YAMLs
- **scripts/python/generate_nodepools.py**: Reads `defs/` → writes `generated/`
- **deploy.sh**: Generates then applies NodePools (with cluster name `sed` replacement)

### arc-runners

ARC runner scale sets for GitHub Actions self-hosted runners. Requires `arc` (controller) and `nodepools` (compute).

- **defs/**: Runner definitions (source of truth) — instance type, CPU, memory, GPU, default max runners
- **templates/runner.yaml.tpl**: Multi-doc template: Helm values + job pod hook ConfigMap
- **generated/**: Auto-generated ARC runner configs (from defs + template)
- **scripts/python/generate_runners.py**: Reads `defs/` + template + `clusters.yaml` → writes `generated/`
- **scripts/python/validate_runner_qos.py**: Pre-deploy validation (Guaranteed QoS, requests == limits) — invoked via `uv run`
- **deploy.sh**: Generates configs, validates QoS, applies the per-runner hook ConfigMaps and Helm releases (enforces `arc` module presence)

### logging

Centralized log collection — Grafana Alloy DaemonSet (pod logs + journal) and Events Deployment → Grafana Cloud Loki.

- **pipelines/base.alloy**: Base Alloy River config — pod log collection, journal collection, loki.write output
- **helm/**: Helm values for DaemonSet mode Alloy (`alloy-logging-values.yaml`) and Events Deployment (`alloy-events-values.yaml`)
- **scripts/python/assemble_config.py**: Assembles base pipeline + per-module `stage.match` blocks into a ConfigMap
- **deploy.sh**: Secret-gated Alloy install (DaemonSet for logs + Deployment for events)

### buildkit

Dual-architecture container build service with HAProxy load balancing.

- **kubernetes/base/**: BuildKit namespace, Deployments (arm64 + amd64), Services, HAProxy (least-connections LB), NetworkPolicy
- **scripts/python/generate_buildkit.py**: Generates Deployments + NodePools from instance type specs and `clusters.yaml` config
- **deploy.sh**: Generates manifests, applies k8s resources, deploys Karpenter NodePools, waits for rollout

Build-node tuning (NVMe RAID0, registry mirrors, CPU tuning) is not a separate script in this module — it comes from the Karpenter EC2NodeClass userData baked into the generated `nodepools.yaml` and from cluster-wide DaemonSets in `base/kubernetes/`.

### harbor-cache-recovery

Automated recovery from Harbor proxy cache corruption (stale manifests, size mismatches). CronJob scans all pods for ImagePullBackOff errors with cache corruption indicators and purges the corrupted repository from Harbor so the next pull re-fetches from upstream. Never deletes pods.

- **scripts/python/harbor_cache_recovery.py**: Core logic — scan pods, parse image references, map to Harbor proxy cache projects, purge via Harbor API
- **docker/**: Container image (Python 3.12 alpine, lightkube + requests)
- **kubernetes/**: RBAC (ClusterRole for pod list) and CronJob with config placeholders. Note: no `kustomization.yaml` — `deploy.sh` applies the manifests imperatively after `sed` placeholder substitution.
- **deploy.sh**: Content-addressed image build, push to Harbor via port-forward, manifest apply with config substitution

### monitoring

Grafana Alloy in metrics mode, scraping cluster + module endpoints and remote-writing to Grafana Cloud Mimir.

- **helm/**: Alloy values for the metrics pipeline
- **kubernetes/**: namespace, DCGM exporter DaemonSet + custom-metrics ConfigMap (`dcgm-exporter/`), PrometheusRule alert CRDs (`alerts/` — ARC, GPU, infra, network-pressure, node-compactor, nodelocaldns, harbor-cache-recovery, zombie-cleanup), and ServiceMonitor / PodMonitor manifests (`monitors/`)
- **logging/pipeline.alloy**: Log routing rules for the monitoring module's own pods
- **deploy.sh**: Installs `kube-prometheus-stack` (CRDs, node-exporter, kube-state-metrics, Prometheus Operator — Prometheus/Grafana/AlertManager are disabled), Prometheus Pushgateway, the `monitors/` kustomize (ServiceMonitors / PodMonitors / `alerts/` PrometheusRules) after the CRDs land, and — gated on the `grafana-cloud-credentials` secret — Grafana Alloy as the metrics push pipeline to Grafana Cloud

### pypi-cache

Per-CUDA-slug nginx + pypiserver fanout backed by a shared EFS wheelhouse fed by an external wheel-build pipeline via S3. Used by runners to install PyTorch wheels without hitting public PyPI.

- **terraform/**: S3 wheel-cache bucket + IRSA roles (has its own `backend.tf`)
- **kubernetes/**: namespace, ServiceAccount, EFS StorageClass + PVC, NetworkPolicy
- **scripts/**: pod resource computation, manifest generation, cache-enforcer SNI matching
- **docker/**: nginx + njs merge handler image
- **generated/**: per-CUDA-slug Deployments + Services
- **deploy.sh**: applies kustomize, deploys NodePools, generates and applies per-slug Deployments

### cache-enforcer

DaemonSet that pins runners' DNS / SNI traffic at PyPI and similar hostnames to the in-cluster pypi-cache service. Pure kubernetes module — has only `kubernetes/kustomization.yaml` (configmap + daemonset) and `tests/`, no `deploy.sh`, no terraform.

### zombie-cleanup

CronJob that reaps orphaned ARC runner pods left behind by aborted GitHub Actions workflows.

- **docker/**: Container image
- **scripts/**: Cleanup logic
- **kubernetes/**: RBAC + CronJob (no `kustomization.yaml` — `deploy.sh` applies the manifests imperatively after `sed` placeholder substitution)
- **deploy.sh**: Image build/push and manifest apply

### arc-runners-h100, arc-runners-b200

Delegate modules for H100 / B200 GPU runner scale sets. Ship only `defs/`, `generated/`, and a 13-line `deploy.sh` that exports `ARC_RUNNERS_DEFS_DIR` / `ARC_RUNNERS_OUTPUT_DIR` / `ARC_RUNNERS_MODULE_NAME` then `exec`s the parent `arc-runners/deploy.sh`. See "Delegate modules" above.

### nodepools-h100, nodepools-b200

Delegate modules for H100 / B200 GPU NodePools. Same pattern as the arc-runners delegates but with an extra `scripts/` directory holding a per-SKU node-setup script, exporting `NODEPOOLS_*` env vars and delegating to `nodepools/deploy.sh`.
