# Recent Changes Agent — Phase 2 Findings

## Summary

The bug is a **first-day regression directly caused by the new `arc-cbr-production-uw1` cluster bootstrap on 2026-05-19**. The cluster's nodepools generator correctly skips `g5/g6/p4d/c7a/r7a` (all marked `exclude_regions: [us-west-1]` in `modules/nodepools/defs/*.yaml`), but `modules/arc-runners/scripts/python/generate_runners.py` had **no equivalent exclusion logic**, so the matching runner scale sets (including `mt-l-x86aavx2-189-704-a10g-8`) were rendered with `proactive_capacity: 5` / no `maxRunners` cap (i.e. unbounded) and registered with GitHub. The fix landed as PR #597 ("Zero-out advertised capacity for region-excluded runners") merged at **2026-05-20T00:14:19Z — over 3 hours AFTER the job in question dispatched at 20:55Z** and 90 min after it failed. Helm revisions on the cluster confirm: revision 2 at 21:32 UTC (pre-fix) had `proactive_capacity=5`/no maxRunners; revision 3 at 00:25 UTC (post-fix deploy) has both forced to 0.

## Findings

### Finding 1: arc-runners generator never honored `exclude_regions` (root cause of advertised capacity)
- **Confidence**: high
- **Severity**: critical
- **Detail**: `grep -rn exclude_regions modules/arc-runners/` returned empty before PR #597. The nodepools generator (`modules/nodepools/scripts/python/generate_nodepools.py:491` `_is_excluded_for_region`) correctly skips fleet defs whose `exclude_regions` list contains the cluster region. The arc-runners generator (`modules/arc-runners/scripts/python/generate_runners.py`) referenced `region` only for `runner_group` override (line 188-194) and had no parallel check. Result: in any region where a nodepool was excluded, the matching ARS still got rendered/published. This was a latent bug until 2026-05-19 because every other live cluster had no `exclude_regions` matches (arc-cbr-production = us-east-2; arc-staging never had g5 excluded for it). `arc-cbr-production-uw1` is the **first cluster to ever hit a non-empty `exclude_regions` set** (g5, g6, p4d, c7a, r7a all list `us-west-1`).

### Finding 2: `arc-cbr-production-uw1` cluster + first deploy happened the same day as the bug
- **Confidence**: high
- **Severity**: critical
- **Detail**: PR #580 (`16b35d3` 2026-05-19 18:48:39Z PDT 11:48) added the cluster entry to `clusters.yaml`. First successful prod deploy targeting both clusters ran at 2026-05-19T17:20:18Z (run 26113481917, head `a59d5c3`, sha BEFORE the cluster entry was merged — must have been a manual targeted ue2 deploy). The first deploy of uw1 effectively happened during the run that created ARS objects. Live ARS `mt-l-x86aavx2-189-704-a10g-8` has `creationTimestamp: 2026-05-19T19:57:55Z` — that is the moment the buggy scale set first appeared, registered with GitHub, and advertised capacity. The job in question was created at 20:55Z, ≈58 min later.

### Finding 3: Two prod deploys to uw1 occurred between cluster creation and the bug job
- **Confidence**: high
- **Severity**: moderate
- **Detail**: Three OSDC prod deploys on 2026-05-19: 17:20→17:43 (sha `a59d5c3`, before uw1 cluster entry), 21:26→22:05 (sha `29fc70e` — both clusters, after uw1 entry), and 2026-05-20 00:18→01:07 (sha post-#597, includes the fix). The 19:57 ARS creation timestamp aligns with a deploy that's not in the dispatch list — likely the controlling sequence was: cluster created via manual workflow, ARSs created at 19:57, helm rev 2 happened in the 21:26 deploy (matches timestamps 21:32). PR #588 "Add stale scale-set detection and healing" was merged just before (17:55Z) — suggests heavy operator activity during initial cluster bring-up. The job dispatched between 21:32 (rev 2 deploy) and 22:05 (deploy_ue2 finished) — capacity was advertised the entire time.

### Finding 4: PR #597 is the explicit fix for this exact bug, merged AFTER it occurred
- **Confidence**: high
- **Severity**: critical
- **Detail**: PR #597 ("Zero-out advertised capacity for region-excluded runners") body literally says: *"in arc-cbr-prod-uw1, the g5/g6/p4d/c7a/r7a defs carry exclude_regions: [us-west-1] because AWS does not offer those instance families in the region. Without this change, the matching runner scale sets register with GitHub and advertise unbounded capacity, so GitHub routes jobs (e.g. mt-l-x86aavx2-189-704-a10g-8) to a cluster where they pend forever."* Author: Huy Do. Merge commit: `ed35920a`. Merged 2026-05-20T00:14:19Z. The bug job ran 20:55Z–~23:34Z, so the fix was reactive. PR #598 followed at 01:50:36Z to plug the same hole in `runner_max_map.py` (used by `just resume-runners`).

### Finding 5: HUD failure fallback (d5d94fb / chart .10) is a **contributor**, not the root cause
- **Confidence**: medium
- **Severity**: moderate
- **Detail**: Commit `d5d94fb` (2026-05-15) added `HUDFailureMultiplier=3` so when the HUD API is unreachable the desired placeholders become `ProactiveCapacity * 3` instead of `ProactiveCapacity + 0_queued_jobs`. Rolled to production via PR #577 (2026-05-17T15:57Z, head `9f6f264`) which bumped `chart_version` from `.9` to `.10`. **All ARSs in arc-cbr-production-uw1 run that chart version**: `gha-runner-scale-set-controller-0.14.1-jeanschmidt.10` (helm list confirmed). The HUD probably could not reach the new cluster's listener during first-day instability (or, more plausibly, the fresh us-west-1 listener had no HUD anchor yet). On HUD failure, the formula amplifies advertised capacity 3x — turning `proactive_capacity=5` into 15 advertised pairs. This is **not the root cause** (the capacity should have been 0 to start with) but it likely amplified the pull. Earlier fallback (assuming 0 queued jobs) would have masked the bug because advertised capacity = proactive — but proactive was already 5, so jobs would still have routed.

### Finding 6: Runner def itself unchanged for weeks, instance_type stable
- **Confidence**: high
- **Severity**: minor
- **Detail**: `modules/arc-runners/defs/l-x86aavx2-189-704-a10g-8.yaml` last touched 2026-05-06 (`db9e433` adds `max_burst_capacity: 30`). Before that, `2026-04-29` (`b216358` proactive capacity PoC adds `proactive_capacity: 5`). Created 2026-03-19 (`fcc4df1`). So the def has been advertising `proactive_capacity: 5 / max_burst_capacity: 30` to every cluster that deployed it for weeks. **The bug is entirely about which clusters the def is deployed to, not the def itself.**

### Finding 7: All other arc-runner-related changes in last 30 days are not implicated
- **Confidence**: medium
- **Severity**: minor
- **Detail**: Reviewed `b216358` (proactive capacity PoC, 2026-04-29), `a91d3fd` (proactive metrics, 2026-04-30), `db9e433` (max_burst_capacity, 2026-05-06), `c042586` (scale up defaults, 2026-05-07), `e66ba76` (prepare-job timeout 2h, 2026-05-07), `9f6f264` (chart bump to .10, 2026-05-17), `8ad4356` (stale scale-set healing, 2026-05-19), `15804d7` (pause_runners flag, 2026-05-19). None add region-exclusion logic. `8ad4356`'s `just heal-arc` recipe is a manual remediation tool — irrelevant to capacity reporting. `15804d7`'s `pause_runners: true` flag is the cluster-wide kill switch and could have been used as a workaround but wasn't applied to uw1 before the bug.

### Finding 8: Fork chart `.10` (jeanschmidt.10) and controller pod versions are CONSISTENT with code
- **Confidence**: high
- **Severity**: minor
- **Detail**: Live cluster runs `ghcr.io/jeanschmidt/gha-runner-scale-set-controller:0.14.1-jeanschmidt.10` (51 image refs returned, all identical). Controller helm release `arc` is at chart `gha-runner-scale-set-controller-0.14.1-jeanschmidt.10` deployed 2026-05-19T19:54:45Z. All 47 runner helm releases at the same chart. So the running fork code matches commit `d5d94fb` HEAD (HUD failure fallback + everything before it). No fork commit drift.

## Timeline (chronological)

| Date (UTC) | Source | Change | Suspect rating |
|---|---|---|---|
| 2026-03-19 | osdc commit `fcc4df1` | Creates l-x86aavx2-189-704-a10g-8.yaml runner def (g5.48xlarge) | low |
| 2026-04-21 | osdc commit `5349f6e` | Copy arc-cbr-production from ciforge (= us-east-2 prod, no us-west-1 yet) | low |
| 2026-04-21 | osdc commit `bba3f86` | Require known instance types in fleet derivation | low |
| 2026-04-23 | osdc commit `74f9204` | Add optional maxRunners support to arc-runners generator | low |
| 2026-04-23 | fork commit `6c56d56` | Add capacity-aware placeholder pod pre-warming | low |
| 2026-04-28 | fork commits `adf2791..1d0ff0f` | Point images/charts to personal fork, placeholder PoC | low |
| 2026-04-29 | osdc commit `b216358` | Add proactive_capacity PoC (sets l-x86aavx2-189-704-a10g-8.proactive_capacity=5) | **medium — preloads non-zero capacity into the def** |
| 2026-04-29 | fork commits `2c07a07..69136c7` | Split runner/workflow placeholders, drop runner-class, Prometheus metrics, chart .6 | low |
| 2026-04-30 | osdc commit `a91d3fd` | Add metrics for proactive capacity | low |
| 2026-04-30 | fork commit `f6c56d3` | Add MaxBurstCapacity and MaxRunners headroom | low |
| 2026-04-30 | fork commit `89a5113` | Bump chart versions to jeanschmidt.7 | low |
| 2026-05-01 | fork commits `a3c294a`,`d219a11` | proactive_capacity_max_runners merge, detect/replace broken placeholder pairs | low |
| 2026-05-01 | osdc commit `ed01267` | Add H100 nodepool with us-west-1 exclude_regions (first OSDC use of exclude_regions) | medium — first time exclude_regions exists in nodepools (note: existing g5/g6 already had it) |
| 2026-05-05 | fork commit `4714643`,`30c1a10` | Require runner-class in workflow affinity / release_enforcement | low |
| 2026-05-06 | osdc commit `db9e433` | Add max_burst_capacity to ARC runners (l-x86aavx2-189-704-a10g-8 gets 30) | **medium — amplifies advertised capacity** |
| 2026-05-07 | osdc commit `c042586` | Scale up OSDC defaults for higher load | low |
| 2026-05-13 | osdc commit `9ec4c0e` | Bump runner-container-hooks v0.8.11 (OOM hardening) | low |
| 2026-05-15 | osdc commit `11bb0eb` | Bump runner-container-hooks v0.8.12 (forward GHA cancel into pod) | low |
| 2026-05-15 14:23 PDT | fork commit `d5d94fb` | HUD failure fallback: multiplier=3x — replaces 0-queued fallback with 3x amplification on HUD failure | **medium — amplifies pull when HUD is unreachable** |
| 2026-05-17 15:57Z | osdc PR #577 `9f6f264` | Bump ARC fork chart to 0.14.1-jeanschmidt.10 (rolls d5d94fb into all clusters) | medium |
| 2026-05-18 05:04Z | osdc PR #576 `a6b4c8c` | IPv6-only pod networking + runner-container-hooks v0.8.13 | low |
| 2026-05-18 23:37Z | osdc PR #591 `a59d5c3` | Fix arm64 silicon mismatches for t4g/m7g labels | low |
| 2026-05-19 11:48 PDT (18:48Z) | osdc PR #580 `16b35d3` | **Add arc-cbr-production-uw1 entry to clusters.yaml** | **critical — first cluster to hit g5/g6/p4d/c7a/r7a exclude_regions** |
| 2026-05-19 11:48 PDT | osdc PR #581 `d81ce00` | Support cluster-level runner_group override in arc-runners generator (touches the very file that should also have had region exclusion) | high — proves arc-runners generator was being modified for uw1 without region exclusion check |
| 2026-05-19 11:49 PDT | osdc PR #583 `b69e86e` | Support cluster-level capacity_reservation_ids override in nodepools | low |
| 2026-05-19 11:51 PDT | osdc PR #587 `009ab80` | Move all prod capacity reservation IDs into clusters.yaml | low |
| 2026-05-19 11:52 PDT | osdc PR #584 `d7e7815` | Allow osdc-deploy-prod to target either prod cluster (input added) | high — this is what enabled deploying uw1 |
| 2026-05-19 11:53 PDT | osdc PR #585 `29fc70e` | Plan both prod clusters on PRs sequentially | low |
| 2026-05-19 17:20Z | run 26113481917 (sha `a59d5c3`) | OSDC: Deploy production (manual workflow_dispatch, presumably ue2 only — pre-uw1-entry) | low |
| 2026-05-19 17:53Z | osdc PR #588 `8ad4356` | Add stale scale-set detection and healing (suggests bring-up debugging) | low |
| 2026-05-19 17:57Z | osdc PR #589 `2cf7d78` | Add cluster recreation runbook | low |
| 2026-05-19 18:18Z | osdc PR #593 `15804d7` | Add pause_runners cluster-level flag (a kill switch — was never set for uw1) | low |
| 2026-05-19 19:57:55Z | live cluster | ARS `mt-l-x86aavx2-189-704-a10g-8` created with `proactive_capacity=5`, no maxRunners cap (helm rev 1) → registered with GitHub at scale-set-id 879 | **critical — bug now possible** |
| **2026-05-19 20:55Z** | GitHub Actions | Job 76835590399 dispatched to runner type mt-l-x86aavx2-189-704-a10g-8 in uw1 | **bug manifests** |
| 2026-05-19 21:26:23Z | run 26126318639 (sha `29fc70e`) | OSDC: Deploy production (manual, both clusters) — uw1 deploy at 21:28:28Z–21:40:11Z, smoke 21:42 success | n/a (re-deploys, no fix) |
| 2026-05-19 21:32:51Z | live cluster | helm revision 2 of arc-l-x86aavx2-189-704-a10g-8 — still `proactive_capacity=5`, no maxRunners | n/a |
| 2026-05-19 ~21:34Z | GitHub Actions | Job started on broken runner | n/a |
| 2026-05-19 ~23:34Z | GitHub Actions | Job failed at "Initialize containers" after ~2h | n/a |
| **2026-05-20 00:14:19Z** | osdc PR #597 `ed35920a` | **Zero-out advertised capacity for region-excluded runners** (the explicit fix, authored by Huy Do) | **critical — root-cause fix** |
| 2026-05-20 00:18:29Z | run | OSDC: Deploy production (sha post-#597) | n/a |
| 2026-05-20 00:25:21Z | live cluster | helm revision 3 of arc-l-x86aavx2-189-704-a10g-8 — now `proactive_capacity=0`, `maxRunners=0` | n/a (fix applied) |
| 2026-05-20 01:50:36Z | osdc PR #598 | Make resume-runners respect region-excluded scale sets (plugs the same hole in `runner_max_map.py`) | low |

## Open Questions

1. The 17:20Z deploy (run 26113481917) targeted prod with `a59d5c3` head SHA — that's BEFORE `16b35d3` (cluster entry). Was the uw1 cluster created via `tofu apply` outside the GH workflow (manual local run from someone's laptop)? Live ARS creationTimestamp 19:57:55Z suggests a separate event between 17:43 (first deploy ended) and 21:26 (second deploy started).
2. Did the HUD failure multiplier actually trigger for this specific listener in the bug window? If yes, advertised capacity was `proactive_capacity * 3 = 15` for this runner type — would corroborate with metrics. Listener logs for arc-l-x86aavx2-189-704-a10g-8 between 19:57Z and 21:34Z need inspection (Phase 6 logs agent).
3. Was the runner group `arc-cbr-prod-uw1` created on the pytorch org BEFORE the deploy? The PR #580 description lists it as a prerequisite ("1. Org runner group arc-cbr-prod-uw1 created at pytorch"). If yes — when, and were existing repos still routing to "default" group? This affects whether GitHub even should have routed the job.
4. Did the test for PR #597 (`TestLoadExcludedInstanceTypes`) catch this scenario in unit tests before merge? If yes, why wasn't a similar test mandated when PR #580 added the new cluster?

## Recommended Next Steps

1. **Phase 6 (logs)**: pull arc-systems controller logs and arc-runners listener logs for `mt-l-x86aavx2-189-704-a10g-8` between 19:57Z (ARS created) and 23:34Z (job ended). Check (a) advertised `maxRunners`/`runners.count` sent to GitHub, (b) `gha_capacity_*` Prometheus metrics — `gha_capacity_hud_requests_total{result="failure"}` and `gha_capacity_advertised_max_runners` time series in this window, (c) whether the HUD failure path was hit.
2. **Phase 6 (state)**: look at the original helm rev 1 values for `arc-l-x86aavx2-189-704-a10g-8` (creation time 19:57Z). `helm get values --revision 1` to confirm what was advertised when the ARS first registered.
3. **Phase 8 (culprits)**: weight PR #580 cluster-add + missing arc-runners generator exclude_regions handling as the structural root cause (75%), HUD failure fallback (d5d94fb) as an amplifier (15%), and runner_group override (PR #581) as adjacent context (5%); reserve 5% for "runner group routing on pytorch GitHub Org allowed a job intended for `default` to reach `arc-cbr-prod-uw1`".
4. **Code follow-ups (not for this investigation, but worth flagging in the final write-up)**: a cluster smoke test should fail if any registered ARS has a backing instance type that has no NodePool in the cluster. This would have caught the missing exclusion logic on day one of uw1.
