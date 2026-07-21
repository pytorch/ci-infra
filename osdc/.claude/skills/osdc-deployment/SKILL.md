---
name: osdc-deployment
description: >
  OSDC deployment workflow, just recipes, base deploy order, module deploy order,
  clusters.yaml configuration, Terraform architecture, smoke tests, justfile conventions.
  Applies to ~/meta/ci-infra/osdc.
  Load when deploying, adding modules, or modifying deploy scripts.
---

# OSDC Deployment & Operations

## How Deployment Works

`clusters.yaml` defines every cluster and its modules. The justfile reads it via `scripts/cluster-config.py`.

```bash
just setup                                       # Install Python dependencies (uv sync); mise tools are auto-installed from mise.toml on first `mise exec`
just clean                                       # Remove caches, init artifacts, .venv
just list                                        # Show all clusters and modules
just show <cluster>                              # Inspect cluster config
just kubeconfig <cluster>                        # Configure kubectl (uses scripts/kubeconfig-lock.sh)
just bootstrap <cluster>                         # Create S3 state bucket + DynamoDB
just bootstrap-all                               # Bootstrap all clusters
just deploy <cluster>                            # Full deploy (base + modules)
just deploy-base <cluster>                       # Base only
just deploy-module <cluster> <module> [force]    # Single module; force="" sets HELM_FORCE_UPGRADE=1
just plan <cluster>                              # Read-only `tofu plan` for base + every TF-backed module (CI-safe)
just deploy-history <cluster>                    # List deploy audit ConfigMaps in osdc-system namespace
just deploy-status <cluster> [name]              # Human-readable status of recent deploys
# ^ Both read the deploy-audit ConfigMaps written by every just deploy/deploy-base/deploy-module
#   into osdc-system. For the ConfigMap schema, raw kubectl queries, cross-cluster version diffs,
#   stuck-deploy detection, and failure triage, see the "Deploy-Audit ConfigMaps" section of the
#   osdc-cli-debugging skill.
just recycle-nodes <cluster>                     # Terminate Karpenter nodes (reprovisioned on demand)
just test                                        # Run all unit tests (95% per-file coverage required)
just test-compactor <cluster>                    # Run node-compactor e2e tests (base/node-compactor/tests/e2e/)
just test-janitor <cluster>                      # Run image-cache-janitor e2e tests (base/kubernetes/image-cache-janitor/tests/e2e/)
just smoke <cluster>                             # Run smoke tests against a deployed cluster
just integration-test <cluster> [args...]        # Run full integration test via integration-tests/scripts/python/run.py. CI passes --skip-drain --skip-smoke --skip-compactor
just generate-arc-runners <cluster>              # Generate ARC scale-set YAMLs (no apply)
just lint                                        # Lint all code (shell, Python, Docker, k8s, tf)
just lint-fix                                    # Auto-fix lint issues (ruff, shfmt, tofu fmt, taplo)
just analyze-utilization                         # Analyze runner-to-node packing efficiency
just simulate-cluster                            # Monte Carlo cluster simulation for CI load
just taint-nodes <cluster>                       # Taint ARC runner nodes with NoSchedule for graceful refresh
just untaint-nodes <cluster>                     # Reverse taint-nodes
just drain-runners <cluster>                     # Patch all ARC AutoscalingRunnerSets to maxRunners=0, taint runner nodes, wait for in-flight pods to finish (default 1h; tune with OSDC_DRAIN_TIMEOUT_SECS)
just resume-runners <cluster>                    # Restore maxRunners from defs/ files and untaint nodes (out-of-band recovery; normal cutover path is `just deploy`)
just remove-module <cluster> <module>            # Uninstall all osdc.io/module=<module> resources + matching arc-runners helm releases. Run `just drain-runners` first for runner modules.
just heal-arc-check <cluster> [name]             # Read-only: detect ARC scale-set-id drift (controller, ARS, listener, ERS). Exit 1 if drift found.
just heal-arc <cluster> [name]                   # Detect + recover ARC scale-set-id drift (controller restart, ARS re-registration, listener/ERS cascade).
just load-test <cluster>                         # Run load test against a cluster
just workload-test <cluster>                     # Run production workload test
```

Internal recipes (prefixed `_`, called from other recipes; documented here because they expose load-bearing behavior):

- `_deploy-harbor <cluster>` — called from `deploy-base`. Reads `harbor_role_arn`, `harbor_s3_bucket`, `harbor_s3_access_key_id`, `harbor_s3_secret_access_key` from base tofu outputs; installs the Harbor Helm chart; applies the standalone Harbor PDB manifest (chart has no native PDB support); then calls `_configure-harbor-projects`.
- `_configure-harbor-projects` — bootstraps Harbor proxy-cache projects via the Harbor REST API. Optionally consumes `harbor-dockerhub-credentials` and `harbor-github-credentials` secrets in `harbor-system` to authenticate proxy-cache pulls against Docker Hub / ghcr.io (avoids anonymous rate limits). Create those secrets out-of-band before deploy if needed.

### Environment variables that control deploy behavior

- `OSDC_CONFIRM=yes|no|ask` (default `ask`) — non-interactive confirmation for `just deploy` and `just deploy-base` plans.
- `OSDC_TAINT_NODES=yes|no|ask` (default `ask`) — auto-taint ARC runner nodes after `just deploy`. The deploy recipe consults this only when `recycle_karpenter_nodes != true`; under recycle, tainting is skipped unconditionally (nodes are already being destroyed).
- `OSDC_SMOKE=yes|no|ask` (default `ask`) — auto-run smoke tests at end of `just deploy`.
- `OSDC_DRAIN_TIMEOUT_SECS=<seconds>` (default `3600`) — wait budget for `just drain-runners` while in-flight runner pods finish before declaring stragglers and exiting non-zero.
- `OSDC_UNTAINT_NODES=yes|no` (default `yes`) — controls whether `just resume-runners` removes the refresh taint from runner nodes at the end. Set `no` to leave the taint in place for a separate `just untaint-nodes` step. Mirrors `OSDC_TAINT_NODES` on the `drain-runners` side.
- `OSDC_CONFIRM_BASE=destroy` — required *in addition to* `OSDC_CONFIRM=yes` for `scripts/destroy-cluster.sh` to non-interactively destroy base infrastructure. Not consumed by `just deploy`; included here because it sits on the same operational surface.
- `OSDC_RESOLVER_READONLY=1` — set by the `smoke` recipe around its `generate-arc-runners` preflight so the runner-image resolver reads the newest cached entry from the lock ConfigMap instead of mutating it or hitting GitHub. Anyone running `generate-arc-runners` standalone for testing should NOT set this unless they explicitly want the cached behavior.
- `HELM_FORCE_UPGRADE=1` — passed by `deploy-module` when invoked with the `force` arg; modules' deploy.sh use this to force Helm upgrades.
- `AGENT_ENVIRONMENT=true` — agent mode for `just lint`, `just test`, `just smoke`, `just generate-arc-runners`: suppresses output on success, prints only on failure. Useful for sub-agents.
- `OSDC_ROOT`, `OSDC_UPSTREAM`, `CLUSTERS_YAML` — exported by `deploy-module`, `deploy-base`, `drain-runners`, `resume-runners`, `smoke`, etc., into the environment of every module `deploy.sh`. `OSDC_ROOT` is the consumer repo root (often equal to `OSDC_UPSTREAM`); `OSDC_UPSTREAM` is the upstream OSDC checkout providing shared scripts and modules. Module scripts that source shared helpers (`mise-activate.sh`, `state-config.sh`, `kubectl-apply.sh`, etc.) MUST use `OSDC_UPSTREAM` so they work both in standalone and consumer-override setups.

### Base deploy order (always)

1. **Terraform** — VPC, EKS, Harbor S3 bucket + IAM role + S3 access keys (all produced by `modules/eks/terraform`, parameterized, no per-env dirs)
2. **Mirror images** — Harbor bootstrap images to ECR (Harbor can't cache itself)
3. **Base k8s** — StorageClass, NVIDIA plugin, performance tuning (`kubectl apply -k`)
4. **node-taint-remover** — Generates the `node-taint-remover-lib` ConfigMap in `kube-system` from `lib/taint_remover.py` (generated at apply time to prevent drift). Mounted into DaemonSets (cache-enforcer, registry-mirror-config, node-performance-tuning) so they can remove their own startup taint after init.
5. **ENIConfigs** — One AZ-named ENIConfig CR per AZ, rendered from `private_subnets_by_az` (base terraform output). **Inert by design under IPv6-only EKS — VPC CNI Custom Networking is unsupported in IPv6 mode.** Retained on the deploy path as the rollback fallback for IPv4 + Custom Networking. Has its own `deploy.sh` because it must read the terraform output at apply time.
6. **Harbor** — Pull-through cache (caches docker.io, ghcr.io, nvcr.io, registry.k8s.io, quay.io, public.ecr.aws). `_deploy-harbor` reads `harbor_role_arn`, `harbor_s3_bucket`, `harbor_s3_access_key_id`, `harbor_s3_secret_access_key` from base outputs.
7. **Node compactor** — Taints underutilized Karpenter nodes for consolidation (if enabled)
8. **Image Cache Janitor** — Cleans up unused cached images on nodes
9. **NodeLocal DNSCache** — Per-node CoreDNS DaemonSet (iptables-mode interception). Has its own `deploy.sh` (NOT in `base/kubernetes/kustomization.yaml` — same pattern as image-cache-janitor) because it must resolve the live `kube-dns` Service ClusterIP at apply time and substitute it into Corefile + DaemonSet args.

### Module deploy order (per clusters.yaml list order)

Each module may have:

| File | Purpose | Applied by justfile |
|------|---------|-------------------|
| `terraform/main.tf` | AWS resources (optional) | `tofu apply` with cluster vars |
| `kubernetes/kustomization.yaml` | K8s manifests (optional) | `kubectl apply -k` |
| `deploy.sh` | Custom deploy logic (optional) | Called with `(cluster-id, cluster-name, region)` |

The justfile auto-detects which of these exist and runs them in order: terraform -> kubernetes -> deploy.sh.

`deploy-module` exports three env vars consumed by every module `deploy.sh`: `OSDC_ROOT` (consumer repo), `OSDC_UPSTREAM` (upstream OSDC checkout), and `CLUSTERS_YAML` (path to the active clusters.yaml). It also resolves the module directory with consumer-first precedence: if `$OSDC_ROOT/modules/$MODULE` exists, it wins over `$OSDC_UPSTREAM/modules/$MODULE`. This is how downstream consumers override upstream modules without forking the repo. See `docs/modules.md` for the full module contract.

### clusters.yaml drives everything

There is no hardcoded staging/production concept. Each cluster is an independent installation with its own config. `clusters.yaml` controls everything: infrastructure sizing, component tuning (Harbor replicas, Karpenter PDB, ARC log level), module-specific settings (BuildKit replicas, runner GitHub config, max runner counts), and module list.

Adding a new cluster: add an entry to `clusters.yaml` with region, cluster_name, state_bucket, base config, component tuning, module config, and module list. Then `just bootstrap <cluster-id>` + `just deploy <cluster-id>`.

Adding a module to a cluster: append the module name to the cluster's `modules` list.

To create a new module: make a directory under `modules/`, add any of the files above, and list the module name in `clusters.yaml` for the target cluster.

Reading cluster config in scripts: `uv run scripts/cluster-config.py <cluster-id> <dot.path> [default]`. Supports nested paths (e.g., `harbor.core_replicas`). Falls back to `defaults:` section, then optional third argument.

**`recycle_karpenter_nodes: true`** (cluster-level config, e.g. `meta-staging-aws-uw1`, `meta-staging-aws-ue1`): when set, `just deploy` calls `just recycle-nodes "$CLUSTER"` automatically after modules deploy and **skips** the ARC node-tainting step (tainting is pointless when nodes are being destroyed).

**`proactive_capacity_max: <int>`** (cluster-level): caps the warm-pool placeholder pairs the runner-set scaler will provision ahead of demand. Staging clusters set this to `0` to disable pre-provisioning entirely. See `docs/arc-fork-build-deploy.md` for how this is plumbed into the controller (`CAPACITY_AWARE_PROACTIVE_CAPACITY`).

## Justfile Kubeconfig Convention (MANDATORY)

**Every just recipe that interacts with a live cluster MUST call `just kubeconfig <cluster>` before doing any kubectl/helm work.** This prevents operators from accidentally running commands against the wrong cluster.

The `kubeconfig` recipe wraps `aws eks update-kubeconfig` via `scripts/kubeconfig-lock.sh` — a directory-based mutex (atomic `mkdir`) that serializes concurrent kubeconfig updates and prevents races between parallel deploys. The wrapper enforces a 90s acquisition timeout and breaks stale locks older than 45s.

Note: `just plan` does NOT call `just kubeconfig` — it is read-only tofu and does no k8s/helm work, so it is intentionally exempt from the kubeconfig contract.

The pattern (in shebang recipes):
```bash
export CLUSTERS_YAML="{{CLUSTERS_YAML}}"
just kubeconfig "$CLUSTER"
```

This applies to: `deploy-base`, `deploy-module`, `recycle-nodes`, `taint-nodes`, `untaint-nodes`, `drain-runners`, `resume-runners`, `_deploy-harbor`, `smoke`, `test-compactor`, `test-janitor`, `integration-test`, `deploy-history`, `deploy-status`, and any future recipe that calls kubectl, helm, or deploy scripts. When adding a new recipe that touches a cluster, always include the `just kubeconfig` step. Do NOT call `aws eks update-kubeconfig` directly — the locking wrapper is required.

## Justfile + Mise Gotcha

Tool versions for the entire repo are pinned in `mise.toml` (tofu, kubectl, helm, crane, awscli, ruff, taplo, shellcheck, shfmt, hadolint, kube-linter, kubeconform, tflint, trivy, uv, python, etc.). The `[settings] auto_install = true` setting means `mise exec` installs missing pinned tools on first use — there is no separate "install tools" command; `just setup` only runs `uv sync` for Python deps.

The justfile uses `set shell := ["mise", "exec", "--", "bash", "-euo", "pipefail", "-c"]` so that mise-managed tools are on PATH for every recipe line.

**Shebang recipes bypass this.** A recipe starting with `#!/usr/bin/env bash` is executed as a standalone script by just, NOT through the configured shell. This means mise-managed tools may not be on PATH inside shebang recipes or scripts they call. Shebang recipes in this repo work around it by sourcing `scripts/mise-activate.sh` as their first line.

For recipes that need mise tools, use non-shebang style (line-by-line with `@` prefix) so they run through `mise exec`. If a shebang recipe is required, source `mise-activate.sh` (or call subscripts via `mise exec -- ./script.sh` explicitly).

## Terraform Architecture

Single parameterized root at `modules/eks/terraform/`. No per-environment directories.

- Variables (`cluster_name`, `aws_region`, `vpc_cidr`, etc.) come from `clusters.yaml` -> justfile -> `tofu -var=`
- Backend configured at `tofu init` via `-backend-config` (bucket, key, region)
- Each cluster gets its own state: `s3://<state_bucket>/<cluster-id>/base/terraform.tfstate`
- Modules with their own terraform get: `s3://<state_bucket>/<cluster-id>/<module>/terraform.tfstate`. Module terraform also receives `state_bucket` and `cluster_id` as vars.

## Smoke Tests (MANDATORY)

Post-deploy validation that a cluster is actually healthy. Run via `just smoke <cluster>` or automatically at the end of `just deploy` (controlled by `OSDC_SMOKE=yes|no|ask`).

Smoke tests verify that what was deployed is actually working: Helm releases are deployed, pods are Running, DaemonSets have all pods ready, required secrets and service accounts exist, AWS resources (IAM roles, SQS queues) are present, and Karpenter NodePools / ARC runner scale sets match their definition files. They do NOT test application-level behavior — just that the deployment landed correctly.

**Architecture**: Each module owns its own smoke tests, co-located at `<module>/tests/smoke/`. A root `tests/smoke/conftest.py` provides shared fixtures (batch-fetched cluster state, cluster config, CLI helpers) that all module tests inherit via pytest's conftest discovery. `just smoke <cluster>` reads `clusters.yaml`, finds enabled modules, collects their `tests/smoke/` directories, and runs pytest across all of them. The recipe also unconditionally pulls smoke tests from `base/**/tests/smoke` AND `modules/eks/tests/smoke` (EKS is always deployed as part of base, regardless of `clusters.yaml.modules`). Adding a module with smoke tests = they're discovered automatically. Moving a module = its tests move with it.

**Preflight: ARC runner YAML pre-generation.** Before any pytest worker starts, the `smoke` recipe iterates the cluster's enabled modules and runs `just generate-arc-runners "$CLUSTER"` for every `arc-runners` / `arc-runners-*` variant (canonical + per-GPU-arch), parameterized via `ARC_RUNNERS_DEFS_DIR` / `ARC_RUNNERS_OUTPUT_DIR` / `ARC_RUNNERS_MODULE_NAME`. This regeneration MUST happen once up front — previously a test fixture shelled out per-test and raced under pytest-xdist. Anyone debugging "why does smoke regenerate before tests run" is looking at this step.

**When changing ANY module or base component, you MUST update its smoke tests to cover the change.** If the component doesn't have smoke tests yet, add them. Smoke tests are the safety net that catches deployment regressions — they only work if they stay current.

- Smoke test files live in `<component>/tests/smoke/test_*.py`
- Shared helpers live in the `tests/smoke/helpers/` package:
  - `helpers/cli.py` — `run_kubectl`, `run_helm`, `run_aws` (proxy-bypass-aware)
  - `helpers/filters.py` — pure in-memory filters over batch fixtures
  - `helpers/nodes.py` — node-churn helpers (unstable nodes, disruption taints)
  - `helpers/retry.py` — `retry_with_backoff` primitive
  - `helpers/k8s_asserts.py` — DaemonSet / Deployment readiness assertions
  - `helpers/remote.py` — Mimir / Loki query helpers with skip-on-network-failure semantics
  - `helpers/__init__.py` re-exports everything (`from helpers import X` works without naming the submodule)
- Each module's `tests/smoke/conftest.py` re-exports the root conftest: `from smoke_conftest import *`
- DaemonSet checks use `assert_daemonset_healthy()` which tolerates mismatches caused by nodes in transition (new, NotReady, or being deleted). Deployment checks use `assert_deployment_ready()` which has a 90-second rollout-completion window followed by the standard `retry_with_backoff` schedule once the rollout finishes but replicas still aren't ready
- **Retry primitive**: `retry_with_backoff(check, refresh=None, delays=None)` is the canonical retry abstraction for smoke tests. Retries only on `AssertionError` — other exceptions propagate immediately. Optional `refresh` callable runs between attempts to re-fetch live state. Re-raises the LAST `AssertionError` after exhaustion.
- **Backoff schedule** (resolved at module load time from `os.environ.get("CI")`):
  - **Local** (default): 6 attempts total — 1 initial + 5 retries. Sleeps `[10, 17, 28.9, 49.1, 83.4]` seconds. Worst-case sleep budget per assertion: ~188s.
  - **CI** (`CI=true`, set by Buildkite/GHA/etc.): 8 attempts total — extended to `[10, 17, 28.9, 49.1, 83.4, 141.78, 241.03]` to give slow recoveries more time. Worst-case sleep budget: ~571s per assertion.
- **When writing new smoke tests**: use `retry_with_backoff` for any "wait for X to be ready" pattern — do NOT hand-roll `for _ in range(N): time.sleep(D)` loops.

## Unit Tests & Coverage

`just test` discovers `test_*.py` files across the repo, runs them with pytest-xdist (`-n auto`), and enforces a **per-file coverage threshold of 95%**. Any source file under the test dirs whose coverage drops below 95% will fail the run, even if pytest itself passed. Add or extend tests when you change source files — bumping global coverage is not enough.

Exclusions and gotcha around smoke tests:

- Excluded entirely: `tests/e2e/`, `*/base/*/tests/smoke/`, `*/modules/*/tests/smoke/` (live-cluster suites — run via `just smoke <cluster>`).
- **Included**: the project-root `tests/smoke/` directory, because it hosts unit tests for the smoke-helpers package itself (e.g. `tests/smoke/helpers/test_retry.py`). The recipe runs those tests but **exempts them from the 95% per-file coverage gate** (the helpers' full coverage comes from live-cluster smoke runs).

## Module-Specific Deployment Notes

### EKS Terraform State Key Gotcha

The eks module uses `<cluster-id>/base/terraform.tfstate` as the state key (historical, predates module extraction). Don't assume the state key matches the module name `eks`. Modules reading eks outputs via `terraform_remote_state` (e.g., karpenter reads OIDC provider, node role, SG, subnets) depend on this key.

### Base Node Group — Hidden Behaviors

The `aws_eks_node_group "base"` resource at `osdc/modules/eks/terraform/modules/eks/main.tf` has two non-obvious behaviors that matter when changing `base_node_count` or `base_node_instance_type` in `clusters.yaml`.

#### `base_node_count` is half-applied by tofu

`lifecycle.ignore_changes = [scaling_config[0].desired_size]` means tofu updates `min` and `max` but NOT `desired`. After bumping `base_node_count`, you MUST manually run:

```
NO_PROXY="$NO_PROXY,.eks.amazonaws.com" no_proxy="$no_proxy,.eks.amazonaws.com" \
aws eks update-nodegroup-config \
    --cluster-name <cluster_name> \
    --nodegroup-name <cluster_name>-base-nodes \
    --region <region> \
    --scaling-config minSize=N,maxSize=N,desiredSize=N
```

Skipping this leaves the ASG at the OLD `desired_size`. Symptom: pods go Pending despite the new count being "applied".

#### `base_node_instance_type` change triggers an in-place rolling replacement

EKS treats an `instance_types` change as a managed node group update: every node is cordoned, drained, and replaced.
- Honors PDBs for 15 minutes (the `update_config` timeout)
- Then forces eviction because `force_update_version = true`
- Blast radius controlled by `base_node_max_unavailable_percentage` in `clusters.yaml` (default 33%, staging 100%)

Monitor the rolling update with:
```
aws eks list-updates --cluster-name <cluster_name> --nodegroup-name <cluster_name>-base-nodes --region <region>
aws eks describe-update --cluster-name <cluster_name> --nodegroup-name <cluster_name>-base-nodes --region <region> --update-id <ID>
```

For one-time migrations (e.g. switching to a larger instance family), follow the cluster-specific playbook if one exists at the repo root — disruption coordination, manual `desired_size` bump, and rollback steps live there, not here. If no playbook exists for your migration, write one before starting.

**Cluster recreation runbook**:
- `docs/cluster-recreation.md` — operator-facing runbook for destroying and recreating an existing OSDC cluster (used for any change that touches an immutable `aws_eks_cluster` property: IP family, VPC, CIDR, encryption_config, etc.). Cluster recreates are required because EKS does not support in-place IPv4↔IPv6 conversion. Automation: `scripts/destroy-cluster.sh <cluster-id>` (requires `OSDC_CONFIRM=yes` + `OSDC_CONFIRM_BASE=destroy` for non-interactive destroy).

### CRD Ordering — Monitoring Module

Monitoring deploy order is strict: (1) namespace + DCGM DaemonSet via kustomization, (2) kube-prometheus-stack Helm (installs CRDs + exporters), (3) `monitors/` applied AFTER CRDs exist, (4) Alloy conditionally. Applying ServiceMonitor/PodMonitor resources before kube-prometheus-stack installs the CRDs will fail.

### Cache-Enforcer Dependency

cache-enforcer depends on Harbor (base) and pypi-cache module. Without pypi-cache deployed, pip installs in runner jobs fail entirely (traffic blocked but no cache). Deploy pypi-cache before or alongside cache-enforcer.

### clusters.yaml Config Keys Reference

`clusters.yaml` itself is the source of truth — the section below is a curated subset highlighting load-bearing or non-obvious keys. For the full schema (every `defaults:` and every per-cluster override), read `clusters.yaml` directly.

**Cluster-level keys** (set under `clusters.<id>:`, no fallback to `defaults:`):

- `region` — AWS region for the cluster (e.g. `us-west-2`).
- `cluster_name` — EKS cluster name (used in IAM roles, ECR repo names, kubeconfig alias).
- `state_bucket` — S3 bucket holding terraform state for THIS cluster (`s3://<state_bucket>/<cluster-id>/...`).
- `recycle_karpenter_nodes: true|false` — when true, `just deploy` runs `just recycle-nodes` after modules and skips ARC taint-nodes.
- `access_config:` — EKS authentication mode + cluster-admin access entries:
  ```yaml
  access_config:
    authentication_mode: API_AND_CONFIG_MAP
    cluster_admin_role_names:
      - osdc_gha_prod
  ```
  Consumed by `scripts/cluster-config.py` into tofu vars. Without this, no IAM role gets cluster-admin via access entries.

**Karpenter:**
```yaml
karpenter:
  replicas: 4           # Controller replica count
  log_level: info
  pdb_enabled: true
  pdb_min_available: 1
```

**Harbor:**
```yaml
harbor:
  core_replicas: 4
  registry_replicas: 4
  nginx_replicas: 6
  pdb_max_unavailable: 1     # Default: conservative production protection for core/registry/nginx
  # staging clusters (meta-staging-aws-uw1, meta-staging-aws-ue1) override to "100%" (string, quoted)
  # to effectively disable — staging runs 1 replica each
```

Standalone PDB manifests are applied by `_deploy-harbor` after the Helm install (Harbor chart has no native PDB support). See `osdc-harbor` skill for selector details and known limits.

**ARC controller:**
```yaml
arc:
  chart_version: "0.14.1-jeanschmidt.17"   # Must match controller + runner charts (minor mismatch deletes ARS). Fork chart from jeanschmidt/actions-runner-controller. Check clusters.yaml for the current value.
  runner_image_tag: "2.334.0"
  replica_count: 4
  log_level: info
  controller_cpu_request: "1"
  controller_cpu_limit: "4"
  controller_memory_request: "2Gi"
  controller_memory_limit: "4Gi"
```

**BuildKit:** Defaults provide only `replicas_per_arch: 12`. All other knobs are per-cluster overrides:
```yaml
defaults:
  buildkit:
    replicas_per_arch: 12     # Used when a cluster omits per-arch replica counts
clusters:
  my-cluster:
    buildkit:
      amd64_instance_type: m6id.24xlarge
      amd64_replicas: 32
      amd64_pods_per_node: 2
      arm64_instance_type: m7gd.16xlarge
      arm64_replicas: 8
      arm64_pods_per_node: 4
```
Per-arch override keys are separate (`amd64_*` and `arm64_*`) — there is no flat `pods_per_node` key.

**Node Compactor:** All config under `node_compactor:` key. Knobs configurable via `clusters.yaml`: `enabled`, `interval_seconds` (20), `dry_run`, `min_nodes` (1), `min_node_age_seconds` (900), `capacity_reservation_nodes` (0), `max_uptime_hours` (48). Other knobs (`taint_rate`, `fleet_cooldown`, `spare_capacity_nodes`, `spare_capacity_ratio`, `spare_capacity_threshold`) are internal Python defaults in the compactor source, NOT configurable via clusters.yaml.

**Other defaults blocks** (curated — see `clusters.yaml` `defaults:` for the full list and current values):

- `coredns.replicas` — CoreDNS replica count.
- `nodepools.{gpu_consolidate_after,cpu_consolidate_after,baremetal_consolidate_after,gpu_disruption_budget,cpu_disruption_budget}` — Karpenter NodePool churn knobs.
- `monitoring.{grafana_cloud_url,grafana_cloud_read_url}` — Grafana Cloud Mimir push/read endpoints.
- `logging.grafana_cloud_loki_url` — Grafana Cloud Loki push endpoint.
- `alloy_chart_version` — Alloy Helm chart version (shared by monitoring + logging Alloy DaemonSets).
- `keda.chart_version` — KEDA operator Helm chart version (used by the `keda` module; consumed by buildkit autoscaling).
- `zombie_cleanup.{enabled,pending_max_age_hours,running_max_age_hours,dry_run}` — orphan-pod GC tuning.
- `pypi_cache.{instance_type,cuda_versions,python_versions,target_architectures,target_manylinux}` — PyPI wheel cache matrix (see `osdc-pypi-cache` for slug semantics; `cuda_versions` and `python_versions` are mandatory for this module).

### ARC Module — No Terraform

Unlike most modules, `modules/arc/` has no terraform phase — it is pure k8s/helm. Don't look for terraform state or try `just tf-*` recipes for ARC.

### Pypi-Cache — Two Terraform Roots

The pypi-cache module has TWO terraform roots; only one runs as part of `just deploy`:

- `modules/pypi-cache/terraform/main.tf` — per-cluster IRSA role, IAM policies, etc. **Auto-applied by `just deploy-module pypi-cache`** (the standard `deploy-module` flow detects `terraform/main.tf` and runs `tofu init && tofu apply` on it).
- `modules/pypi-cache/terraform/wheel-cache-bucket/` — standalone tofu root for the shared S3 bucket (`pytorch-pypi-wheel-cache`). This is a one-time, account-wide resource, **NOT part of `just deploy`**. Run `tofu init && tofu apply` manually in this sub-directory if the bucket needs to be created or updated.

### Pypi-Cache — Required clusters.yaml Keys

`cuda_versions` and `python_versions` are NOT module defaults. They must be configured in `clusters.yaml` under `defaults.pypi_cache` or per-cluster `clusters.<id>.pypi_cache`. Omitting them produces zero CUDA slugs and zero deployments. `cuda_slug()` normalizes versions to major.minor only: `"12.8.1"` → `cu128` (matches PyTorch convention).

## CI Workflows

OSDC is driven from CI via GitHub Actions workflows living in the **parent repo** (`ci-infra/.github/workflows/`), NOT inside `osdc/`. There is no `osdc/.github/` directory.

Key workflows (all callable as reusable workflows via `workflow_call` from the orgs that ship from this repo):

| Workflow | What it runs |
|----------|-------------|
| `osdc-lint.yml` | `just lint` |
| `osdc-test.yml` | `just test` |
| `osdc-pre-merge.yml` | Lint + test on PR |
| `osdc-plan-prod.yml` | `just plan <cluster>` (read-only tofu plan; CI-safe) |
| `osdc-deploy-prod.yml` | Per-cluster prod deploy. Calls `_osdc-deploy.yml` |
| `_osdc-deploy.yml` | Reusable: `just lint && just test` → `just deploy <cluster>` → `just smoke <cluster>` → `just integration-test <cluster> --skip-drain --skip-smoke --skip-compactor`. Pre-flight checks can be skipped with the `skip_lint_test` input (firefighting only). |
| `_osdc-plan.yml` | Reusable: `just plan <cluster>` and tee to `plan.txt` |
| `_osdc-slow-tests.yml` | Reusable: `just load-test`, `just test-compactor`, `just test-janitor` |
| `osdc-capacity-report.yml` | `just simulate-cluster` + `just analyze-utilization`, periodic |

When adding a new `just` recipe that CI should run, plumb it through the corresponding reusable workflow.

The OSDC-rooted CI jobs — the `deploy`/`smoke`/`integration` jobs in `_osdc-deploy.yml`, plus `osdc-drain.yml` and `osdc-undrain.yml` — set up their toolchain via the local composite action `./.github/actions/osdc-setup` (installs just + mise + `uv sync`). Its setup steps are retried on transient failures by **retry-by-duplication** (`continue-on-error` on the first attempt plus a copy gated on `steps.<id>.outcome == 'failure'`), chosen over a retry action deliberately: GitHub Actions has no native step retry, and a retry action would run unpinnable third-party code on the deploy / merge-queue path. `actions/checkout` and `aws-actions/configure-aws-credentials` are intentionally NOT wrapped because they already retry internally.
