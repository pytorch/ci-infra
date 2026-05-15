# Multi-region prod cluster: us-west-1 HA + scale-out

Status: proposal, pending feasibility verification on staging.

## Goal

Deploy a second production OSDC EKS cluster in **us-west-1** alongside the
existing `arc-cbr-production` (us-east-2). The two clusters must:

1. Advertise the **same `runs-on` runner labels** (e.g. `mt-l-x86iavx512-46-85`)
   so jobs queued at `github.com/pytorch` can be picked up by either cluster
   with no workflow changes.
2. Run **active/active**: GitHub routes incoming jobs to whichever scale set
   has free capacity. If one region/cluster goes down, the other keeps
   draining the queue (HA fallout).

## How runner labels are composed (recap)

`modules/arc-runners/templates/runner.yaml.tpl`:

```
runnerScaleSetName: "{{RUNNER_NAME_PREFIX}}{{RUNNER_NAME}}"
```

- `RUNNER_NAME_PREFIX` ← `clusters.<id>.arc-runners.runner_name_prefix`
  (`mt-` for prod, `c-mt-` for staging).
- `RUNNER_NAME` ← filename under `modules/arc-runners/defs/*.yaml`.

If the new cluster sets the same `runner_name_prefix` and ships the same
`defs/`, every scale set advertises the identical `mt-…` label. ARC then
needs GitHub to accept multiple scale sets registering under the same name
in the same `githubConfigUrl` + `runnerGroup`. **This is the single
load-bearing assumption** of this design and must be validated on staging
before any prod work.

## Staging feasibility test (Phase 0 — gating)

Stand up `arc-staging-uw2` in a different region (us-west-2 suggested), point
it at the same `pytorch/pytorch-canary` GitHub config, and prove duplicate
scale-set names work end-to-end.

### clusters.yaml entry

```yaml
arc-staging-uw2:
  region: us-west-2
  cluster_name: pytorch-arc-staging-uw2
  recycle_karpenter_nodes: true
  state_bucket: ciforge-tfstate-arc-staging-uw2
  access_config:
    authentication_mode: API_AND_CONFIG_MAP
    cluster_admin_role_names: [osdc_gha_staging]
  base:
    base_node_count: 2
    base_node_instance_type: m5.4xlarge
    base_node_max_unavailable_percentage: 100
    single_nat_gateway: true
    vpc_cidr: "10.16.0.0/16"          # non-overlap with arc-staging (10.0.0.0/16)
  coredns: { replicas: 2 }
  harbor:
    core_replicas: 1
    registry_replicas: 1
    nginx_replicas: 1
    pdb_max_unavailable: "100%"
  karpenter:
    replicas: 1
    log_level: debug
    pdb_enabled: false
    pdb_min_available: 0
  arc:
    replica_count: 1
    log_level: debug
    controller_cpu_request: "500m"
    controller_cpu_limit: "1"
    controller_memory_request: "1Gi"
    controller_memory_limit: "1Gi"
  node_compactor: { dry_run: true }
  nodepools:
    gpu_consolidate_after: "10s"
    cpu_consolidate_after: "10s"
  arc-runners:
    github_config_url: "https://github.com/pytorch/pytorch-canary"   # SAME as arc-staging
    github_secret_name: pytorch-arc-staging                          # SAME secret name
    runner_name_prefix: "c-mt-"                                      # SAME → labels collide
  buildkit:
    replicas_per_arch: 2
    arm64_instance_type: m7gd.16xlarge
  pypi_cache: { replicas: 1 }
  modules:
    # Minimum needed to test the routing model. No GPU, no Buildkit, no PyPI.
    - karpenter
    - arc
    - nodepools
    - arc-runners
    - monitoring
    - logging
```

### Execution

```bash
just bootstrap arc-staging-uw2
# Plant the existing pytorch-arc-staging GitHub App secret
# into arc-staging-uw2's arc-runners namespace (same App, same key).
just deploy arc-staging-uw2
```

### Validation gates (stop at first failure)

**Gate A — GitHub accepts duplicate scale-set names.**
After `arc-runners` installs, on `github.com/pytorch/pytorch-canary` →
Settings → Actions → Runners, every `c-mt-…` name appears twice. ARC
listener logs on both clusters are healthy. If GitHub dedupes, rejects, or
either listener crash-loops on registration, the design is dead — stop.

**Gate B — GitHub routes by capacity.**
Dispatch a workflow with `runs-on: c-mt-l-x86iamx-8-16` ~20 times. Identify
the executing cluster from the runner pod's AZ/region. Expect a roughly
balanced split. One cluster getting 100% means GitHub is sticking to one
scale set — investigate before continuing.

**Gate C — failover.**
Temporarily set `proactive_capacity: 0` on one runner def in cluster 1,
redeploy that cluster's `arc-runners`, dispatch jobs. They should all flow
to cluster 2. Restore.

**Gate D — clean teardown.**
Uninstall a scale set from `arc-staging-uw2` and confirm it deregisters from
GitHub without disturbing the identically-named scale set on `arc-staging`.
This exercises ARC's deregistration scoping (per-installation-id, not
per-name).

If A–D all pass, proceed to Phase 1.

## Phase 1 — prod cluster in us-west-1

### Identity

| Field            | Value                                                         |
|------------------|---------------------------------------------------------------|
| `cluster_id`     | `arc-cbr-production-uw1`                                      |
| `cluster_name`   | `pytorch-arc-cbr-production-uw1`                              |
| `region`         | `us-west-1`                                                   |
| `state_bucket`   | `ciforge-tfstate-arc-cbr-prod-uw1` (created in us-west-2)     |
| `vpc_cidr`       | `10.8.0.0/16` (non-overlap with 10.0/16, 10.4/16, 10.16/16)   |
| admin role       | `osdc_gha_prod` (must exist or be created in IAM)             |

### Pre-flight AWS prerequisites (per-region, not shared)

- **Service quotas** in us-west-1: vCPU limits for c7i, m7i, r7i, m7g, m8g,
  g4dn, g5, g6, p5 families. Request raises early.
- **EKS-optimized AMIs**: confirm `base_node_ami_version: v20260318` AL2023
  AMI is published in us-west-1, and that EKS GPU AMIs are available.
- **GPU Capacity Reservations**:
  - **H100 (p5)**: reservation IDs in us-west-1 — collect these for use in
    the per-cluster override (see below).
  - **B200 (p6-b200)**: none in us-west-1 → B200 is excluded from this
    cluster.
- **GitHub App credentials Secret**: reuse the existing
  `pytorch-arc-cbr-production` GitHub App. Plant the same Kubernetes Secret
  (same App, same private key) into the new cluster's `arc-runners`
  namespace after `deploy-base`, before `deploy-module arc-runners`.

### B200: omit from this cluster

Don't add `nodepools-b200` / `arc-runners-b200` to the new cluster's
`modules:` list. The `mt-l-…-b200-…` labels continue to be served only by
us-east-2. No code change required; B200 simply isn't HA. Acceptable
because there is no B200 capacity in us-west-1.

### H100: per-cluster capacity-reservation override (no new module)

Today `modules/nodepools-h100/defs/p5-48xlarge.yaml` hardcodes
`cr-0c3f05dffb85ed832` (a us-east-2 reservation). The new cluster needs
different CR IDs in us-west-1. Two ways to handle this:

**Option A (recommended) — per-cluster override in `clusters.yaml`.**
Extend the schema and teach the nodepools generator to read it:

```yaml
arc-cbr-production:
  # … existing …
  nodepools-h100:
    capacity_reservation_ids: [cr-0c3f05dffb85ed832]

arc-cbr-production-uw1:
  # … existing …
  nodepools-h100:
    capacity_reservation_ids: [cr-XXXXXXXX, cr-YYYYYYYY]  # us-west-1
```

Generator change in
`modules/nodepools/scripts/python/generate_nodepools.py`: after loading
each def, if
`clusters.<id>.<NODEPOOLS_MODULE_NAME>.capacity_reservation_ids` is set,
override `nodepool.capacity_reservation_ids` before rendering. The
`NODEPOOLS_MODULE_NAME` env var (already set by the `nodepools-h100` /
`nodepools-b200` delegators) namespaces the override key naturally. Add
unit tests under `modules/nodepools/scripts/python/`.

Pros: one module, no duplication; reservation IDs live next to the rest of
the cluster config; the same mechanism works for B200 once us-west-1
reservations land later.

Cons: ~30 lines + a test.

**Option B — duplicate the module (`nodepools-h100-uw1`).**
Faster (no code change), but creates a near-identical thin-delegator that
must be kept in sync. AWS rotates reservation IDs periodically (see commit
`767fce5` swapping B200 reservations) — duplication doubles that toil.

**Decision: Option A.** Removes existing reservation-coupling debt while
unblocking this work.

### Region-specific items that won't auto-replicate

- **Harbor pull-through cache** is per-cluster (good — no cross-region
  pulls). Image mirroring runs during `deploy-base`.
- **PyPI wheel cache** is per-cluster with its own EFS wheelhouse. The S3
  wheel feeder upstream of OSDC writes to a single bucket — verify the new
  region can read it, or add S3 replication / a second feeder.
- **Git cache** is per-cluster; central StatefulSet clones independently.
- **Grafana Cloud monitoring/logging**: same tenant; clusters are
  distinguished by `cluster_name` label. No change.

### clusters.yaml entry

```yaml
arc-cbr-production-uw1:
  cluster_name: pytorch-arc-cbr-production-uw1
  region: us-west-1
  state_bucket: ciforge-tfstate-arc-cbr-prod-uw1
  access_config:
    authentication_mode: API_AND_CONFIG_MAP
    cluster_admin_role_names: [osdc_gha_prod]
  base:
    vpc_cidr: "10.8.0.0/16"
  git_cache:
    central_replicas: 15
    central_cpu_request: "10"
    central_cpu_limit: "15"
    central_memory_request: "10Gi"
    central_memory_limit: "50Gi"
  node_compactor:
    min_node_age_seconds: 900
  buildkit:
    arm64_instance_type: c7gd.16xlarge
    amd64_instance_type: m6id.24xlarge
    pods_per_node: 2
  arc-runners:
    github_config_url: "https://github.com/pytorch"               # SAME as us-east-2 prod
    github_secret_name: pytorch-arc-cbr-production                # SAME secret name
    runner_name_prefix: "mt-"                                     # SAME → labels collide
  nodepools-h100:
    capacity_reservation_ids: [cr-XXXXXXXX, cr-YYYYYYYY]          # us-west-1 reservations
  pypi_cache:
    replicas: 10
  modules:
    - karpenter
    - arc
    - nodepools
    - nodepools-h100         # H100 only — B200 not yet available in us-west-1
    - arc-runners
    - arc-runners-h100
    - buildkit
    - pypi-cache
    - cache-enforcer
    - zombie-cleanup
    - harbor-cache-recovery
    - monitoring
    - logging
```

### Deploy

```bash
just bootstrap arc-cbr-production-uw1
just deploy-base arc-cbr-production-uw1
# Plant the GitHub App Secret (pytorch-arc-cbr-production) into the new
# cluster's arc-runners namespace, using the same App private key as us-east-2.
just deploy arc-cbr-production-uw1
```

Pre-deploy gates (from `CLAUDE.md`): `just lint` and `just test` green.

### Post-deploy validation

1. `kubectl get autoscalingrunnersets -n arc-runners` on the new cluster —
   every scale set in `RUNNING` state.
2. On the prod runner group page, every `mt-…` scale set appears twice
   (one entry per cluster), both showing recent listener heartbeats.
3. Trigger one small PR on `pytorch/pytorch` per shared label class
   (CPU / ARM / GPU). Confirm at least one job lands on the new cluster
   (verify by pod node region/AZ).
4. Smoke tests in `osdc/tests/`.
5. Monitoring + logging flowing into Grafana Cloud tagged with the new
   `cluster_name`.

## Active/active capacity split

Active/active falls out for free from same-label registration — no extra
wiring. Two starting points for `proactive_capacity` split between
clusters:

- **50/50.** Copy `defs/` as-is into both clusters. Doubles warm-pool cost
  but maximizes burst headroom in either region.
- **Skewed toward us-east-2 (incumbent).** First weeks: us-west-1 runs at
  30–50% of prod proactive capacity to keep warm-pool spend in check while
  trust builds. Two ways to express this:
  - Edit `proactive_capacity` per def and accept that defs are now
    cluster-aware (not great).
  - Add a per-cluster multiplier (e.g.
    `clusters.<id>.arc-runners.proactive_capacity_multiplier: 0.5`) and
    apply it in `modules/arc-runners/scripts/python/generate_runners.py`.
    Same shape as the H100 CR override change.

**Recommendation for go-live:** start with 30% us-west-1 / 100% us-east-2
to limit blast radius, then ramp us-west-1 to 100% once a week of
production traffic shows clean routing. Use the multiplier approach so the
ramp is one number in `clusters.yaml`, not a per-def edit.

### Capacity-planning note

Quotas and reservations in us-west-1 must be sized to **absorb 100% of
prod traffic** in the failover case, not just the steady-state share.
Otherwise the "HA" claim doesn't hold during a us-east-2 outage.

## Open questions to resolve before Phase 1

1. **Gate A outcome on staging.** Non-negotiable — everything depends on
   it.
2. **us-west-1 H100 CR IDs** — collect before writing the clusters.yaml
   entry.
3. **us-west-1 service-quota raises** — file early; some GPU families have
   long lead times.
4. **PyPI wheel S3 feeder** — does the existing producer write to a bucket
   that us-west-1 can read, or do we need replication?
5. **Proactive-capacity ramp plan** — confirm the start-at-30% approach
   with the team, or pick the 50/50 default.

## What we are NOT doing in Phase 1

- B200 in us-west-1 (no capacity).
- Cross-region Harbor / PyPI / git-cache sharing (each cluster stands
  alone; this is by design and keeps the failure domains isolated).
- Workflow changes in `pytorch/pytorch` — the whole point of same-label
  registration is that workflows are untouched.

If Gate A fails on staging, the fallback design is a distinct prefix
(e.g. `mt2-`) on us-west-1 plus workflow updates to accept either label —
significantly larger blast radius and out of scope for this doc.
