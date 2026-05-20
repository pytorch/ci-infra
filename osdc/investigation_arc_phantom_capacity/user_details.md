# Investigation: ARC Phantom Capacity Advertisement on arc-cbr-production-uw1

## Bug Summary (user-reported)

A few jobs ran on the `arc-cbr-production-uw1` cluster for a runner type that the cluster cannot actually provision. The expected behavior of the proactive-capacity feature in the ARC fork is:
1. Listener creates placeholder pods (runner + workflow pair) FIRST
2. Wait for placeholders to become live (pods Running)
3. Only THEN advertise capacity by increasing reported max_runners
4. GitHub then assigns jobs because capacity exists

Observed behavior: capacity was advertised and jobs were assigned EVEN THOUGH the underlying instances (g5.48xlarge) are not available in us-west-1, which means placeholders could never have become live.

User's core question: **what mechanism allowed the listener to advertise capacity without live placeholders?**

## Production Safety Rules (MANDATORY for all agents)

- This is a **PRODUCTION** cluster (`arc-cbr-production-uw1`, us-west-1).
- **READ-ONLY operations only. NO side effects.** No `apply`, `delete`, `edit`, `patch`, `scale`, `cordon`, `drain`, `restart`. No `helm install/upgrade/uninstall`. No `tofu apply/destroy/import`. No `aws` write operations.
- **Only interact with `arc-cbr-production-uw1`.** Ignore `arc-staging` and `arc-cbr-production` (us-east-2).
- **GitHub operations: read-only only.** No PR comments, no issue creation, no merges, no edits. Only `gh api` GET, `gh pr view/list`, `gh issue view/list`, `gh run view`, `gh api -X GET`.
- The fork repo at `/Users/jschmidt/meta/actions-runner-controller` is a local clone — read freely, do not commit/push.
- The OSDC repo at `/Users/jschmidt/meta/ci-infra/osdc` — read freely, do not modify state.

If you need to verify behavior that would require a write, document the exact command and stop — return to the orchestrator.

## Concrete Evidence

### The failing job

- **Run URL**: https://github.com/pytorch/pytorch/actions/runs/26122694930
- **Job ID**: 76835590399
- **Job URL**: https://github.com/pytorch/pytorch/actions/runs/26122694930/job/76835590399
- **Job name**: `torchtitan-x-pytorch-test / test-osdc (torchtitan_features_integration, 1, 1, mt-l-x86aavx2-189-704-a10g-8)`
- **Workflow**: `torchtitan-test`
- **Created**: 2026-05-19T20:55:47Z
- **Started**: 2026-05-19T21:34:16Z
- **Completed**: 2026-05-19T23:35:13Z (~2h)
- **Status**: completed / **failure**
- **Failing step**: `Initialize containers` (failed after ~2h)
- **Labels**: `['mt-l-x86aavx2-189-704-a10g-8']`
- **Runner ID**: 42292023
- **Runner name**: `mt-l-x86aavx2-189-704-a10g-8-tfvvr-runner-np5rz`
- **Runner group ID**: 85
- **Runner group name**: `arc-cbr-prod-uw1` ← confirms uw1 cluster handled it

### Cluster config (arc-cbr-production-uw1)

- region: `us-west-1`
- cluster_name: `pytorch-arc-cbr-production-uw1`
- runner_name_prefix: `mt-`
- runner_group: `arc-cbr-prod-uw1` (distinct from us-east-2's `default`)
- github_config_url: `https://github.com/pytorch` (org-scoped)
- modules: karpenter, arc, **nodepools**, nodepools-h100, **arc-runners**, arc-runners-h100, buildkit, pypi-cache, cache-enforcer, zombie-cleanup, harbor-cache-recovery, monitoring, logging
- `arc-runners-b200` is intentionally NOT deployed (no B200 capacity in us-west-1)
- nodepools-h100 has uw1-specific capacity reservation IDs

### Runner def: l-x86aavx2-189-704-a10g-8.yaml

```yaml
runner:
  name: l-x86aavx2-189-704-a10g-8
  instance_type: g5.48xlarge
  disk_size: 150
  vcpu: 189
  memory: 704Gi
  gpu: 8
  proactive_capacity: 5
  max_burst_capacity: 30
```

### NodePool def: g5.yaml

```yaml
fleet:
  name: g5
  arch: amd64
  gpu: true
  exclude_regions:
    - us-west-1
  instances:
    ...
```

**Conclusion from defs**: the `arc-runners` module is deployed in uw1, so the ARS for `l-x86aavx2-189-704-a10g-8` IS generated. But the `nodepools` module's `g5.yaml` def excludes us-west-1, so no g5 NodePool is generated in uw1. Therefore Karpenter cannot provision g5.48xlarge nodes, and runner pods requesting that instance cannot be scheduled — yet a runner DID register and DID pick up the job.

### ARC fork commits (recent)

```
d5d94fb HUD failure fallback: over-provision placeholders
30c1a10 Merge pull request #4 from jeanschmidt/jeanschmidt/release_enforcement
4714643 Require runner-class in workflow affinity
d219a11 Detect and replace broken placeholder pairs
a3c294a Merge pull request #3 from jeanschmidt/jeanschmidt/proactive_capacity_max_runners
```

The proactive-capacity-max-runners and placeholder logic live in this fork. `ghalistener/` is the listener subdirectory.

## Reproduction Status

**Bug already manifested in production logs.** No reproduction needed at this phase — phase 6 (logs/state) will pull the evidence directly from the live cluster and Loki.

The failure mode is observable from outside without any interaction with the cluster:
- GitHub API confirms a job was assigned to runner group `arc-cbr-prod-uw1`
- The runner name pattern `mt-l-x86aavx2-189-704-a10g-8-tfvvr-runner-np5rz` matches the cluster's prefix + the unfulfillable runner def
- `Initialize containers` failed after ~2h, consistent with a runner pod that never properly started or attached

## Investigation Targets (where the truth lives)

1. **ARC fork ghalistener code** (`/Users/jschmidt/meta/actions-runner-controller/ghalistener/`) — proactive capacity, max_runners reporting, placeholder lifecycle
2. **ARC fork controllers** (`/Users/jschmidt/meta/actions-runner-controller/controllers/`) — ARS / ephemeral runner reconciliation
3. **OSDC arc-runners templates** (`modules/arc-runners/templates/runner.yaml.tpl` and the generator) — how `proactive_capacity` and `max_burst_capacity` flow into listener env vars
4. **Live cluster state** (uw1) — pending pods, ARS status, listener pod logs, placeholder state, what max_runners is being reported
5. **Loki logs** for arc-systems namespace (listener + controller)
6. **Mimir metrics** for `gha_*` listener metrics, ARS capacity
7. **Recent fork commits** — the `proactive_capacity_max_runners` PR and `HUD failure fallback: over-provision placeholders` commit specifically

## Open Questions for Investigation

1. Is the runner scale set actually created in uw1 for `l-x86aavx2-189-704-a10g-8`?
2. What is the listener reporting as `max_runners` for this ARS?
3. Are there placeholder pods stuck in Pending state? For how long?
4. Does the proactive-capacity code have a path that advertises capacity when placeholders fail/never start? (e.g. the "HUD failure fallback: over-provision placeholders" commit)
5. Is there a timeout/escape hatch that releases capacity even when placeholders haven't become live?
6. Does the listener distinguish "placeholder live" from "placeholder created"?
7. How does the runner pod actually start (init containers, sidecars, scheduling gates) — did it bypass the no-node constraint somehow?
