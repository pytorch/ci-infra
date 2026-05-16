# Multi-region prod cluster: us-west-1 HA + scale-out

Status: design validated via code review; staging cannot test the prod
design end-to-end due to a GitHub API scope difference (see "Phase 0
outcome" below). Ready to proceed to Phase 1.

## Goal

Deploy a second production OSDC EKS cluster in **us-west-1** alongside the
existing `arc-cbr-production` (us-east-2). The two clusters must:

1. Advertise the **same `runs-on` runner labels** (e.g. `mt-l-x86iavx512-46-85`)
   so jobs queued at `github.com/pytorch` can be picked up by either cluster
   with no workflow changes.
2. Run **active/active**: GitHub routes incoming jobs to whichever scale set
   has free capacity. If one region/cluster goes down, the other keeps
   draining the queue (HA fallout).

## How runner labels and groups compose

`modules/arc-runners/templates/runner.yaml.tpl`:

```
runnerScaleSetName: "{{RUNNER_NAME_PREFIX}}{{RUNNER_NAME}}"
runnerGroup:        "{{RUNNER_GROUP}}"
```

- `RUNNER_NAME_PREFIX` ← `clusters.<id>.arc-runners.runner_name_prefix`
  (`mt-` for prod, `c-mt-` for staging).
- `RUNNER_NAME` ← filename under `modules/arc-runners/defs/*.yaml`.
- `RUNNER_GROUP` ← `runner_group` on the runner def, with a hard guard in
  `generate_runners.py:187-191` that **forces `default`** whenever
  `githubConfigUrl` is repo-scoped (`https://github.com/<org>/<repo>`).
  Custom groups only work for org-scoped (`https://github.com/<org>`) and
  enterprise-scoped URLs.

GitHub's uniqueness rule for scale sets is `(name, runnerGroupId)`. The
same name in two different runner groups is allowed. Workflows that say
`runs-on: <name>` match runners from **any** group with that name that the
target repo is allowed to use, so jobs route by capacity across groups.

**HA active/active mechanism (the design this doc proposes):**

1. Both clusters set the same `runner_name_prefix` and ship the same
   `defs/`, so every scale set advertises an identical `mt-…` label.
2. Each cluster registers in its **own** runner group at the
   `pytorch` org level (e.g. `arc-cbr-prod-ue2` and `arc-cbr-prod-uw1`).
   No collision because the `(name, group)` tuple differs.
3. Both groups are made visible to `pytorch/pytorch` (and any other repos
   that target these labels).
4. GitHub distributes incoming jobs across the two scale sets that match
   the label — true active/active.

Failover is implicit: if one cluster's listener goes offline, jobs route
to the surviving group.

## Phase 0 outcome — staging cannot test the prod design

A staging cluster `arc-staging-uw2` was deployed in us-west-2 (mirror of
`arc-staging` in us-west-1) intending to verify the routing model
end-to-end. Cluster came up; ARC controllers ran; listener pods were
created. But no scale-set duplicates appeared on the GitHub runner page,
even after listeners stabilized.

The cause is at the GitHub API level, not in our code:

- `arc-staging` and `arc-staging-uw2` both use
  `githubConfigUrl: https://github.com/pytorch/pytorch-canary`
  (**repo-scoped**).
- `generate_runners.py:187-191` enforces `runnerGroup = "default"` for all
  repo-scoped URLs because GitHub's runner-scale-set API only supports
  custom groups for org or enterprise scope.
- Both clusters therefore tried to register the same names in the same
  `default` group → name collision → second registration was
  rejected/dedup'd silently.

This is a property of GitHub's API + repo-scope, not a property of our
design. Production uses `githubConfigUrl: https://github.com/pytorch`
(**org-scoped**), where custom runner groups are supported and the
runner-group-per-cluster design works as documented.

### Validation alternative: code-reading

Without an end-to-end staging test, the active/active mechanism is
validated by:

- GitHub's documented behavior: scale-set uniqueness is `(name, groupId)`;
  same names across different groups are allowed; workflow label matching
  spans all visible groups.
- `generate_runners.py` already passes `runner_group` through to the helm
  chart; the chart writes it into the scale-set registration request.
- `arc-cbr-production` (us-east-2) today registers in the `default` group
  (it doesn't set `runner_group`). To use the runner-group-per-cluster
  pattern, we must first move it to a dedicated group (see "Phase 1
  prerequisite" below).

### What also went sideways on staging (not blocking — captured for the record)

- **Cross-arch image-cache-janitor build failed** on the brand-new cluster
  because Alpine had rolled forward `util-linux`, and the GitHub Actions
  amd64 runner doesn't have QEMU registered for the arm64 build leg.
  Fixed in #575 (Dockerfile pin bump + `docker/setup-qemu-action` in
  `_osdc-deploy.yml`). Will hit every fresh cluster deploy until that PR
  lands.
- **VPC CNI IP allocation stalled** on the small 2-node base fleet after
  ~30 listener pods + ambient DaemonSets piled on. Bumping
  `WARM_ENI_TARGET` and restarting `aws-node` partially unstuck it but
  listeners kept churning. Production base fleets are larger (default 6 ×
  m7i.12xlarge) and unlikely to hit this; staging tooling could use a
  follow-up to either enable prefix delegation or right-size the base
  fleet for the listener density it actually hosts.

### Disposition of arc-staging-uw2

The cluster (and its branch `arc-staging-uw2-feasibility`) stays in place
short-term for any further read-only investigation. Plan to tear it down
once Phase 1 lands and the design is in production.

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

### Phase 1 prerequisite 1 — runner group at the pytorch org

The runner-group-per-cluster mechanism needs **one** org-level runner group
created before us-west-1 registers. An org admin (write access on
`pytorch` → Settings → Actions → Runner groups) must:

1. **Create `arc-cbr-prod-uw1`** at
   `https://github.com/organizations/pytorch/settings/actions/runner-groups/new`.
   Set "Repository access" to include `pytorch/pytorch` (and any other
   repos that target `mt-…` labels).

We do **not** need to migrate `arc-cbr-production` (us-east-2) off the
`default` group. The asymmetry is functionally fine:

- us-east-2 keeps registering in `default` (no change).
- us-west-1 registers in `arc-cbr-prod-uw1` (new group).
- For any shared label, GitHub matches across both groups → jobs route
  to whichever cluster has capacity.

Optional cleanup: a follow-up PR can create `arc-cbr-prod-ue2` and move
us-east-2 into it for symmetry. Not required for HA to work.

### Phase 1 prerequisite 2 — cluster-level `runner_group` override

The generator (`generate_runners.py:147`) currently reads `runner_group`
only from each runner def file. To set us-west-1's group cluster-wide
without forking every def, we need a small generator change:

- Read `clusters.<id>.arc-runners.runner_group` from `clusters.yaml`.
- If set, override the value from the def file (subject to the existing
  repo-scope guard at lines 187-191, which doesn't apply to us-west-1
  since prod is org-scoped).
- Add tests.

The change is mechanically similar to the per-cluster
`capacity_reservation_ids` override planned for H100 NodePools — same
pattern, same place to add tests.

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
    runner_name_prefix: "mt-"                                     # SAME → labels match
    runner_group: "arc-cbr-prod-uw1"                              # DISTINCT from us-east-2's "default" → no collision
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
2. On `https://github.com/organizations/pytorch/settings/actions/runner-groups/arc-cbr-prod-uw1`,
   each `mt-…` scale set appears as a row with a healthy listener
   heartbeat. The same names also appear in the `default` group (served
   by us-east-2). Two groups, same names, both online.
3. Trigger one small PR on `pytorch/pytorch` per shared label class
   (CPU / ARM / GPU). Confirm at least one job lands on the new cluster
   (verify by pod node region/AZ). Repeat dispatches should land on
   either cluster roughly in proportion to available capacity.
4. Smoke tests in `osdc/tests/`.
5. Monitoring + logging flowing into Grafana Cloud tagged with the new
   `cluster_name`.

## Active/active capacity split

Active/active falls out for free from the same-label-different-group
registration — GitHub matches `runs-on` across all visible groups and
routes by capacity, no extra wiring. Two starting points for
`proactive_capacity` split between clusters:

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

1. **Org runner group `arc-cbr-prod-uw1` created** by a pytorch org admin
   with repository access including `pytorch/pytorch`.
2. **Cluster-level `runner_group` override landed** in
   `generate_runners.py` (small generator change + tests).
3. **us-west-1 H100 CR IDs** — collect before writing the clusters.yaml
   entry.
4. **us-west-1 service-quota raises** — file early; some GPU families have
   long lead times.
5. **PyPI wheel S3 feeder** — does the existing producer write to a bucket
   that us-west-1 can read, or do we need replication?
6. **Proactive-capacity ramp plan** — confirm the start-at-30% approach
   with the team, or pick the 50/50 default.

## What we are NOT doing in Phase 1

- B200 in us-west-1 (no capacity).
- Cross-region Harbor / PyPI / git-cache sharing (each cluster stands
  alone; this is by design and keeps the failure domains isolated).
- Migrating us-east-2 off the `default` runner group. The asymmetry
  (us-east-2 in `default`, us-west-1 in `arc-cbr-prod-uw1`) is functionally
  equivalent. A follow-up PR can symmetrize later.
- Workflow changes in `pytorch/pytorch` — the whole point of the
  same-label-different-group design is that workflows are untouched.
