# Modules

## What is a module

A self-contained service that layers on top of a base cluster. Modules live under `modules/<name>/` and are enabled per-cluster in `clusters.yaml`.

## Module contract

The justfile auto-detects and runs these in order:

| Phase | File | When |
|-------|------|------|
| 1. Terraform | `terraform/main.tf` | If file exists — `tofu apply` with independent state |
| 2. Kubernetes | `kubernetes/kustomization.yaml` | If file exists — `kubectl apply -k` |
| 3. Custom | `deploy.sh` | If file exists and is executable — called with args |

`deploy.sh` receives three positional arguments:
```bash
$1 = cluster-id      # e.g. "arc-staging"
$2 = cluster-name    # e.g. "pytorch-arc-staging"
$3 = region           # e.g. "us-west-1"
```

A module can use any combination of these. Only `deploy.sh` is strictly needed for simple modules.

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
- Passes `-var="cluster_name=..."` and `-var="aws_region=..."` as minimum vars

Your `variables.tf` must declare at least:
```hcl
variable "cluster_name" { type = string }
variable "aws_region" { type = string }
```

Add more variables as needed. If they need cluster-specific values, extend `clusters.yaml` with module-specific config and update `scripts/cluster-config.py`.

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

If your `deploy.sh` needs values from the base terraform (e.g. VPC ID, OIDC provider ARN):

```bash
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
OSDC_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
CFG="$OSDC_ROOT/scripts/cluster-config.py"
BUCKET=$(python3 "$CFG" "$CLUSTER" state_bucket)

cd "$OSDC_ROOT/modules/eks/terraform"
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
OSDC_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
CFG="$OSDC_ROOT/scripts/cluster-config.py"

if python3 "$CFG" "$CLUSTER" has-module arc; then
    echo "ARC is enabled, deploying runner scale sets"
fi
```

## Existing modules

### eks

Base AWS infrastructure. Always deployed as part of `deploy-base`, not as an optional module.

- **terraform/**: VPC (public/private subnets, NAT gateways), EKS cluster (OIDC, addons), Harbor S3 bucket + IAM
- **images.yaml**: Bootstrap images mirrored to ECR (Harbor components that can't cache themselves)
- **deploy.sh**: Image mirroring via `crane`

### arc

ARC controller for GitHub Actions self-hosted runners.

- **kubernetes/**: `arc-systems` + `arc-runners` namespaces, runner ServiceAccount, LimitRange, hooks ConfigMap, registry mirror config
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
- **scripts/validate-runner-qos.sh**: Pre-deploy validation (Guaranteed QoS, requests == limits)
- **deploy.sh**: Generates configs, validates QoS, applies ConfigMaps + Helm releases (enforces `arc` module presence)

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
- **scripts/node-setup.sh**: NVMe RAID0, registry mirrors, CPU tuning for build nodes
- **deploy.sh**: Generates manifests, applies k8s resources, deploys Karpenter NodePools, waits for rollout
