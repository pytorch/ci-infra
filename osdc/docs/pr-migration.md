## Reorganize arc/ into modular osdc/ layout

Restructures the monolithic `arc/` directory into a modular `osdc/` (Open Source Dev Cloud) layout that supports multiple clusters, projects, and optional module composition — all driven by a single `clusters.yaml`.

### Why

The `arc/` layout was a single-purpose CI runner project hardcoded for two environments (staging/production). As we add new projects (devgpu, remoteexec) on separate clusters, we need shared base infrastructure and composable modules. This reorg makes that possible without code duplication.

### What changed

**Architecture** — `arc/` → `osdc/` with base/modules split:
```
osdc/
├── clusters.yaml          # THE source of truth — all clusters + config
├── base/                  # Shared infra (VPC, EKS, Harbor, git cache)
│   ├── terraform/         # Single parameterized root (no per-env dirs)
│   ├── kubernetes/        # Base k8s resources
│   ├── helm/              # Harbor values
│   └── scripts/           # Bootstrap, image mirroring
└── modules/               # Optional, composable per cluster
    ├── arc/               # ARC controller
    ├── nodepools/         # Karpenter NodePools (pure compute)
    ├── arc-runners/       # ARC runner scale sets
    └── buildkit/          # BuildKit build service
```

**No more staging/production dichotomy** — Every cluster is an independent installation configured entirely through `clusters.yaml`. Component tuning (replicas, log levels, PDB settings) lives in the cluster config with sensible defaults. Deploy scripts read config via `cluster-config.py` dot-path resolution.

**Key behavioral changes from the reorg:**
- Terraform: single parameterized root replaces per-env directories. Backend configured at init time.
- NodePools: generated from compact def files instead of hand-maintained 200-line YAMLs. No Karpenter resource limits (prefer overspend over outage).
- Runners: `generate_runners.py` reads GitHub config directly from `clusters.yaml` (deleted `env-values.yaml`). No runner count limits (prefer overspend over outage).
- Helm deploys: per-installation `--set` flags from cluster config replace per-env values overlay files.
- BuildKit: replicas from cluster config via `kubectl scale`. Stuck-rollout workaround restored.
- Destroy: proper 8-phase teardown (workloads → controllers → infra) instead of bare `tofu destroy`.
- Lint: no longer silently swallows errors.
- QoS validation: pre-deploy check that runner job pods have Guaranteed QoS (requests == limits).
- Python scripts: PEP 723 inline metadata + `uv run` (no more `ModuleNotFoundError`).
- Shebang recipes: `mise-activate.sh` ensures mise-managed tools are on PATH.

### What's NOT changing
- Runner template (`runner.yaml.tpl`): byte-for-byte identical
- Runner definitions: same instance types, CPU, memory, GPU, disk sizes
- Terraform modules (VPC, EKS, Harbor): byte-for-byte identical
- Helm base values: identical (tuning comes from cluster config `--set` flags)
- Node disk sizing: same formula (max_pods × per_pod_disk + 100Gi overhead)
- All existing cluster infrastructure remains untouched

### How new projects plug in

The whole point of this reorg is that new projects get a full EKS cluster with Harbor, Karpenter, git cache, and GPU support *for free* — then layer their own services on top as modules.

**Example: `remoteexec`** (remote execution service)

Say we want a cluster where users can submit compute jobs to run on managed GPU/CPU nodes. No GitHub Actions needed — just Karpenter-provisioned compute and a custom job scheduler.

**Step 1** — Create the module directory:
```
modules/remoteexec/
├── terraform/main.tf      # DynamoDB job queue, IAM roles, S3 results bucket
├── kubernetes/
│   ├── kustomization.yaml
│   ├── namespace.yaml
│   └── scheduler.yaml     # Job scheduler deployment
└── deploy.sh              # Any custom deploy logic
```

**Step 2** — Add the cluster to `clusters.yaml`:
```yaml
  remoteexec-prod:
    region: us-east-1
    cluster_name: pytorch-remoteexec-production
    state_bucket: ciforge-tfstate-remoteexec-prod
    base:
      vpc_cidr: "10.3.0.0/16"
    modules:
      - nodepools            # reuse existing compute provisioning
      - remoteexec           # the new project
```

**Step 3** — Deploy:
```bash
just bootstrap remoteexec-prod
just deploy remoteexec-prod
```

That's it. The base layer handles VPC, EKS, Harbor, Karpenter, GPU drivers, git cache, node performance tuning. The `nodepools` module (already written) handles Karpenter NodePool provisioning. The new module only contains what's unique to remoteexec.

Each project gets its own terraform state, its own k8s namespace, its own module config in `clusters.yaml`. No cross-module coupling. Projects can share a cluster (multiple modules on one cluster) or get dedicated clusters — just change the YAML.

### Test plan
- [x] `cluster-config.py` resolves all nested paths (harbor, karpenter, arc, buildkit, arc-runners) with defaults fallback
- [x] `generate_runners.py arc-staging` produces correct configs (GitHub URL, secret, prefix)
- [x] `generate_runners.py arc-production` falls through to defaults where no cluster override exists
- [x] `generate_nodepools.py` produces NodePools with no `spec.limits` and correct disk sizes (1600Gi, 1100Gi, 900Gi)
- [x] QoS validator passes all 5 runners including GPU (requests == limits, integer CPU)
- [x] Boolean values output as `true`/`false` (not Python `True`/`False`) for Helm compatibility
- [x] All Python scripts compile, all shell scripts pass `bash -n`, clusters.yaml is valid YAML
- [x] `just deploy arc-staging` succeeds end-to-end (Harbor, Karpenter, base k8s, ARC, NodePools, runners, BuildKit)
- [x] BuildKit test workflow (`test-buildkit.yml`) triggers successfully with Harbor `ci-test` project
- [ ] `just deploy arc-production` (after staging validation period)
