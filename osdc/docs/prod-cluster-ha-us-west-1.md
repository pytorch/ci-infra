# Multi-region prod cluster: us-west-1 HA + scale-out

Deploy a second production OSDC EKS cluster (`arc-cbr-production-uw1`) in
us-west-1 alongside the existing `arc-cbr-production` (us-east-2) for
active/active load sharing and regional HA.

## How it works

Both clusters advertise the same `mt-*` runner labels but register their
scale sets in **different runner groups** at the `pytorch` org level.
GitHub's uniqueness rule for scale sets is `(name, runnerGroupId)`, so the
same name in two groups doesn't collide. Workflows that say
`runs-on: mt-…` match runners from any visible group with that name, so
GitHub routes incoming jobs to whichever cluster has capacity. Failover is
implicit — if one cluster's listeners go offline, jobs land on the
surviving group.

| Cluster                   | Region    | Runner group        |
|---------------------------|-----------|---------------------|
| `arc-cbr-production`      | us-east-2 | `default`           |
| `arc-cbr-production-uw1`  | us-west-1 | `arc-cbr-prod-uw1`  |

Note: custom runner groups only work for org-scoped `githubConfigUrl`
(`https://github.com/pytorch`). The repo-scope guard in
`generate_runners.py:187-191` forces `default` for repo-scoped URLs, so
this design wouldn't work on staging (which is repo-scoped at
`pytorch/pytorch-canary`).

## Prerequisites

1. **Runner group at the pytorch org.** Create `arc-cbr-prod-uw1` at
   `https://github.com/organizations/pytorch/settings/actions/runner-groups/new`.
   Set "Repository access" to include `pytorch/pytorch` (and any other
   repos that target `mt-…` labels).
2. **H100 capacity reservations in us-west-1.** Collect the IDs from the
   AWS console; B200 is omitted because there's no B200 capacity in
   us-west-1.
3. **us-west-1 service-quota raises** for the vCPU families this cluster
   uses (c7i, m7i, r7i, m7g, m8g, g4dn, p5). File early — GPU families
   can have long lead times. `g5`, `g6`, `c7a`, `r7a`, and `p4d` are
   not offered in us-west-1; the matching scale sets render with
   `maxRunners: 0` automatically (see [Region-excluded runners](#region-excluded-runners)).
4. **PyPI wheel S3 feeder reachable from us-west-1.** Verify or add
   replication.

The us-east-2 cluster keeps registering in `default`. We don't need to
move it for HA to work — asymmetry is fine.

## clusters.yaml entry

```yaml
arc-cbr-production-uw1:
  cluster_name: pytorch-arc-cbr-production-uw1
  region: us-west-1
  state_bucket: ciforge-tfstate-arc-cbr-prod-uw1
  access_config:
    authentication_mode: API_AND_CONFIG_MAP
    cluster_admin_role_names: [osdc_gha_prod]
  base:
    vpc_cidr: "10.8.0.0/16"                       # non-overlap with 10.0/16, 10.4/16
  node_compactor:
    min_node_age_seconds: 900
  buildkit:
    arm64_instance_type: c7gd.16xlarge
    amd64_instance_type: m6id.24xlarge
    pods_per_node: 2
  arc-runners:
    github_config_url: "https://github.com/pytorch"
    github_secret_name: pytorch-arc-cbr-production
    runner_name_prefix: "mt-"
    runner_group: "arc-cbr-prod-uw1"              # distinct from us-east-2's "default"
  nodepools-h100:
    capacity_reservation_ids:                     # us-west-1 H100 reservations
      - cr-04d3d1d84e127a562
      - cr-09a53051589034fb8
  pypi_cache:
    replicas: 10
  modules:
    - karpenter
    - arc
    - nodepools
    - nodepools-h100        # H100 only — no B200 capacity in us-west-1
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

## Deploy

```bash
just bootstrap arc-cbr-production-uw1
just deploy-base arc-cbr-production-uw1
# Plant the pytorch-arc-cbr-production GitHub App Secret into the new
# cluster's arc-runners namespace (same App, same private key as us-east-2).
just deploy arc-cbr-production-uw1
```

From CI: dispatch `OSDC: Deploy production` from the Actions tab and pick
`arc-cbr-production-uw1`. Approval required via the `osdc-production`
environment.

## Post-deploy validation

1. `kubectl get autoscalingrunnersets -n arc-runners` shows every scale
   set in `RUNNING`.
2. The org's `arc-cbr-prod-uw1` runner group page lists every `mt-…`
   scale set with a healthy listener heartbeat. The same names also
   appear in the `default` group from us-east-2 — two groups, same
   names, both online.
3. Dispatch a small PR on `pytorch/pytorch` per shared label class
   (CPU / ARM / GPU). Confirm at least one job lands on the new cluster
   (check pod node region/AZ).
4. Smoke tests in `osdc/tests/`.

## Capacity ramp

Recommended go-live posture: start us-west-1 at **30%** of us-east-2's
proactive capacity for the first week to limit blast radius, then ramp
to 50/50 once routing looks healthy. Implement via a per-cluster
multiplier (`clusters.<id>.arc-runners.proactive_capacity_multiplier`)
so the ramp is one number to change, not a per-def edit.

us-west-1 quotas and reservations must be sized to absorb **100%** of
prod traffic in the failover case, not just steady-state share.

## Region-specific items that don't auto-replicate

- **Harbor** — per-cluster pull-through cache, populated on first deploy.
- **PyPI wheel cache** — per-cluster EFS wheelhouse; the upstream S3
  feeder must write to a bucket us-west-1 can read.
- **Monitoring/logging** — Grafana Cloud tenant is shared; clusters
  distinguished by `cluster_name` label.

## Region-excluded runners

us-west-1 lacks several instance families AWS offers in us-east-2:
`g5` (A10G), `g6` (L4), `p4d` (A100), `c7a`, and `r7a`. The matching
nodepool defs in `modules/nodepools/defs/` carry `exclude_regions: [us-west-1]`,
so no NodePool is rendered.

The arc-runners generator reads the same `exclude_regions` lists and
forces `maxRunners: 0` and `proactive_capacity: 0` for any runner whose
`instance_type` is in an excluded def. This keeps the scale set present
(so the runner group still exists on GitHub) but prevents jobs from
being routed to a cluster that cannot schedule them. To add or remove
a region exclusion, edit the relevant nodepool def — the runner side
follows automatically.

## Not in scope

- B200 in us-west-1 (no capacity reservation available).
- A10G / L4 / A100 / AMD-CPU runners in us-west-1 (AWS does not offer
  these instance families in the region — see above).
- Cross-region Harbor / PyPI sharing — failure-domain
  isolation is the design intent.
- Workflow changes in `pytorch/pytorch`. The whole point of the
  same-label-different-group design is that workflows don't change.
