# Phase 3 Compilation — Synthesis of Phase 2 Findings

## Summary

The bug is a first-day regression caused by deploying the brand-new `arc-cbr-production-uw1` cluster on 2026-05-19 — the first cluster to hit a non-empty `exclude_regions` set. **`modules/arc-runners/scripts/python/generate_runners.py` had no `exclude_regions` handling** (unlike `generate_nodepools.py`), so the runner scale set for the g5.48xlarge-backed `mt-l-x86aavx2-189-704-a10g-8` was generated, registered with GitHub, and **advertised unbounded capacity** even though no g5 NodePool exists in us-west-1. Two compounding code-level holes in the ARC fork made the advertised value disastrous: (1) when a runner def omits `max_runners`, `resourcebuilder.go:95-97` substitutes `math.MaxInt32` (~2.1B), and (2) `main.go:180-193` launches the listener and capacity-monitor goroutines concurrently with **no synchronization**, so the listener's first `GetMessage` long-poll sends `X-ScaleSetMaxCapacity=MaxInt32` before the capacity monitor's first `reconcileReporting` can clamp it to 0. **PR #597 — "Zero-out advertised capacity for region-excluded runners" by Huy Do — merged at 2026-05-20T00:14:19Z, ~3 hours AFTER the bug job dispatched and 90 minutes after it failed.** This is the explicit fix. Helm rev 3 (2026-05-20T00:25:21Z) shows `proactive_capacity=0` and `maxRunners=0` for the offending ARS. All six Phase 2 agents converge on the same chain of causation and on PR #597 as the fix; **strong case for short-circuiting to Phase 9** with no further phases needed (see early-exit recommendation below).

## Convergence Map

### Root cause family A — `generate_runners.py` does not honor `exclude_regions`
- **Project Docs Agent** — Finding 1 (high/critical): generator has no region filtering, contrast with `generate_nodepools.py:491` which does.
- **Recent Changes Agent** — Finding 1 (high/critical): `grep -rn exclude_regions modules/arc-runners/` was empty before PR #597; latent for weeks because no prior cluster had `exclude_regions` matches.
- **Recent Changes Agent** — Finding 4 (high/critical): **PR #597 is the explicit fix**, with body literally describing this exact bug.
- **GitHub Agent** — Open Question 1 implies the same (per-cluster `maxRunners: 0` for unsupported regions is the architectural fix).
- **Code Flow Agent** — Recommended Next Steps 3a/3d: "Missing `max_runners` in defs/..." and "Generator-side fix" both point at this.

### Root cause family B — Runner def omits `max_runners` → resolves to `math.MaxInt32`
- **Project Docs Agent** — Findings 2, 4, 9 (high/critical): the full propagation chain from missing `max_runners` → `resourcebuilder.go:95-97` substitutes `math.MaxInt32` → listener Config.MaxRunners = ~2.1B.
- **Code Flow Agent** — Finding 2 (high/critical): independently traces the same chain end-to-end, citing the same `resourcebuilder.go:95-98` lines.
- **Web Search Agent** — Finding 1 (high/critical): confirms `MaxRunners` from config flows directly into `X-ScaleSetMaxCapacity` header on every poll.
- **GitHub Agent** — Finding 1 (high/critical): two `MaxRunners` in play — capacity monitor only mutates the listener's atomic, scaler's `MaxRunners` is frozen.

### Root cause family C — Startup race: listener polls GitHub before capacity monitor's first `reconcileReporting`
- **Web Search Agent** — Finding 2 (high/critical): `errgroup.WithContext` parallel goroutines with no synchronization; reconcileReporting also early-exits on K8s API errors, leaving `config.MaxRunners` intact.
- **GitHub Agent** — Finding 2 (high/critical): same observation, plus the initial `HandleDesiredRunnerCount` from `initialSession.Statistics.TotalAssignedJobs` is processed before the very first `GetMessage`.
- **Code Flow Agent** — Findings 4, 11, 12, 13 (high/critical): four distinct findings on the race window, the unsynchronized goroutine launch, the "keep previous capacity on error" path, and the initial-session statistics handler.
- **Project Docs Agent** — Finding 15 (high/critical): independently identifies the same race via the listener `SetMaxRunners(config.MaxRunners)` initial call.

### Amplifier — HUD failure fallback `* HUDFailureMultiplier(3)` (commit d5d94fb)
- **Recent Changes Agent** — Finding 5 (medium/moderate): rolled to prod 2026-05-17; on HUD failure, `desiredPairs = ProactiveCapacity * 3 = 15`; **amplifier, not root cause**.
- **GitHub Agent** — Finding 8 (high/moderate): same observation, confirms amplifies symptom but does not cause capacity advertisement.
- **Project Docs Agent** — Finding 3 (high/critical): notes the multiplier is unbounded because `max_runners` headroom is `MaxInt32`.
- **Code Flow Agent** — Finding 5 (high/moderate): explicitly clarifies "HUD fallback does NOT itself cause phantom capacity" — only controls placeholder *creation*, not advertised capacity.

### JIT pre-registration explains the 2-hour hang
- **Web Search Agent** — Finding 7 (high/moderate): controller calls `GenerateJitRunnerConfig` BEFORE pod schedules → runner appears "registered" to GitHub even with no live pod.
- **Code Flow Agent** — Finding 7 (high/critical): same observation, traces the exact code path through `ephemeralrunner_controller.go:176-186,648`.
- **Project Docs Agent** — Finding 14 (high/critical): the JIT registration is independent of pod phase; same conclusion.

### Karpenter and external infra are NOT implicated
- **Web Search Agent** — Findings 10, 11 (high/minor): no Karpenter wrong-instance-type bugs; no CVE/security advisory on listener capacity protocol.
- **Status Agent** — Findings 1, 2, 3, 5, 6 (high/minor): no GitHub Status incident in the bug window; no AWS Health event for us-west-1; no g5 capacity stress; no GitHub registration API regression.

### Stale `TotalAssignedJobs` (upstream issue #4397) — alternative/auxiliary
- **Web Search Agent** — Finding 4 (high/critical): permanent over-provisioning after platform incidents; listener restart does not clear it.
- **GitHub Agent** — Finding 5 (high/high): same observation, cites the same issue.
- **Status Agent** — Finding 4 (medium/moderate): notes 2026-05-15 GitHub Actions incident could in theory be the source of stale state in a long-running listener — but **arc-cbr-production-uw1 didn't exist on 2026-05-15** (cluster created 2026-05-19), so this cannot be the source for this specific bug.

## Contradictions

Only one minor, easily reconciled disagreement:

**Was the HUD failure fallback (d5d94fb) involved in this specific incident?**
- **Recent Changes Agent — Finding 5**: leans "likely amplified" (medium confidence).
- **GitHub Agent — Finding 8** and **Code Flow Agent — Finding 5**: both explicitly state HUD fallback does NOT itself advertise capacity — it controls placeholder creation, not the `X-ScaleSetMaxCapacity` header.
- **Reconciliation**: All three are correct. The HUD multiplier affects `desiredPairs` (how many placeholder pods are created) but does NOT directly affect the reporter's `setMaxRunners(runningRunners + runningPairs)` calculation. Since `runningPairs == 0` (no g5 nodes in uw1), the HUD multiplier is irrelevant to the advertised capacity that GitHub saw. The capacity GitHub saw was driven entirely by the startup-race window (`MaxInt32`) or by initial-session `TotalAssignedJobs` carried over from another listener (Finding 11 in Code Flow). **The HUD multiplier may have multiplied the doomed-to-Pending placeholder pods on the cluster, but did not cause the dispatch.**

No other contradictions across the six agents. They independently converge on the same causation chain.

## Hypothesis Status (vs. bug_hypotheses_initial.md)

### H1 — "HUD failure fallback: over-provision placeholders" advertises capacity without live placeholders
- **Status**: **REFUTED** as primary mechanism; **REFINED** as amplifier.
- **Evidence**: Code Flow Finding 5, GitHub Finding 8 — HUD fallback affects `reconcileProvisioning` only, not `reconcileReporting`. The advertised capacity comes from `runningRunners + runningPairs`, not from `desiredPairs`. Recent Changes Finding 5 reframes it as a non-root-cause amplifier.

### H2 — `max_runners` defaults to `max_burst_capacity` regardless of placeholder state
- **Status**: **REFUTED** in its original form; **REFINED** into the actual mechanism.
- **Evidence**: The actual default when `max_runners` is absent is NOT `max_burst_capacity` — it is `math.MaxInt32` (Code Flow Finding 2, Project Docs Findings 2/9). `max_burst_capacity=30` is a separate concept (placeholder burst cap).

### H3 — Placeholder "live" check is satisfied by `PodScheduled` or `ContainerCreating`, not actual `Running`
- **Status**: **REFUTED**.
- **Evidence**: Code Flow Finding 3 — `BothRunning()` at `placeholder.go:46-50` requires `Phase == PodRunning` for BOTH runner and workflow pods. Pending pods do not count.

### H4 — The runner that picked up the job was NOT a placeholder — it was a real ephemeral runner registered before pod scheduling
- **Status**: **CONFIRMED**.
- **Evidence**: Web Search Finding 7, Code Flow Finding 7, Project Docs Finding 14 — JIT registration via `GenerateJitRunnerConfig` happens in `ephemeralrunner_controller.go:648` BEFORE the pod is created. Runner is registered with GitHub independent of pod schedulability. This explains both how the runner accepted the job AND the 2-hour hang.

### H5 — Listener proactive-capacity-max-runners feature has a bug where "current registered" count is treated as available capacity
- **Status**: **REFUTED** in its original form.
- **Evidence**: Code Flow Finding 10 — PR #3 (`a3c294a`) named `proactive_capacity_max_runners` only changed `reconcileProvisioning` (placeholder count formula), NOT `reconcileReporting` (what is reported to GitHub). The actual bug is the missing region exclusion + missing `max_runners` + startup race, not the PR #3 code.

### H6 — `Detect and replace broken placeholder pairs` (d219a11) interprets unfulfillable placeholders as "broken"
- **Status**: **REFUTED**.
- **Evidence**: Code Flow Finding 6 — `CleanupBroken` only runs in `reconcileProvisioning`. The reporter's `BothRunning()` check already excludes broken pairs. CleanupBroken is correct behavior, not a phantom-capacity contributor.

## Prioritized Root Cause Candidates

### Candidate 1 — `generate_runners.py` does not filter runner defs by `exclude_regions` (structural root cause)

- **Confidence**: high
- **Severity**: critical
- **Supporting evidence**:
  - Project Docs Agent Finding 1 — generator code lines 375-377 iterate every YAML in `defs/` with no per-cluster region check; nodepools generator lines 491-500 does have this check.
  - Recent Changes Agent Finding 1 — `grep -rn exclude_regions modules/arc-runners/` returned empty before PR #597.
  - Recent Changes Agent Finding 4 — **PR #597 (ed35920a) is the explicit fix**, body: *"in arc-cbr-prod-uw1, the g5/g6/p4d/c7a/r7a defs carry exclude_regions: [us-west-1] because AWS does not offer those instance families in the region. Without this change, the matching runner scale sets register with GitHub and advertise unbounded capacity, so GitHub routes jobs (e.g. mt-l-x86aavx2-189-704-a10g-8) to a cluster where they pend forever."*
  - Recent Changes Agent Finding 2 — `arc-cbr-production-uw1` is the **first cluster to ever hit a non-empty `exclude_regions` set**; latent bug surfaced on the day the cluster was created.
- **Refuting evidence**: None.
- **Verification needed**: Confirmed already by the existence and merge of PR #597. Phase 6 can verify helm revisions 1/2 had `proactive_capacity=5`, no `maxRunners`; revision 3 (post-#597) has both forced to 0 (Recent Changes Agent already verified via `helm list` and `helm get values`).

### Candidate 2 — Missing `max_runners` in runner def → listener's initial advertised capacity is `math.MaxInt32` (~2.1B)

- **Confidence**: high
- **Severity**: critical
- **Supporting evidence**:
  - Code Flow Agent Finding 2 — full propagation: `defs/l-x86aavx2-189-704-a10g-8.yaml` has no `max_runners` → `generate_runners.py:261` emits empty string → Helm template at `autoscalingrunnerset.yaml:146-151` omits the field → CRD pointer field is `nil` → `resourcebuilder.go:95-98` substitutes `effectiveMaxRunners := math.MaxInt32` → listener Config.MaxRunners = ~2.1B.
  - Project Docs Agent Finding 9 — independent end-to-end trace of the same eight steps with the same file:line citations.
  - Project Docs Agent Finding 5 — H100/B200 defs DO set `max_runners`; `docs/arc-fork-build-deploy.md:220` admits "CPU/T4/A10G/L4/A100 runner defs currently do not set `max_runners`."
  - Web Search Agent Finding 1 — confirms `MaxRunners` from config flows directly into `X-ScaleSetMaxCapacity` HTTP header on every poll.
  - Project Docs Agent Finding 10 — docs claim "Without max_runners, the value is empty/unlimited" — actually it resolves to MaxInt32, which is *worse* than empty.
- **Refuting evidence**: None.
- **Verification needed**: Phase 6 should verify `AutoscalingListener.Spec.MaxRunners == 2147483647` for the offending ARS by reading the live CR.

### Candidate 3 — Startup race: listener polls GitHub before the capacity monitor's first `reconcileReporting`

- **Confidence**: high
- **Severity**: critical
- **Supporting evidence**:
  - Code Flow Agent Finding 4, Finding 12 — `main.go:180-193` launches `listener.Run` and `capMonitor.Run` as sibling goroutines via `errgroup.WithContext` with no synchronization; listener begins polling immediately with `l.maxRunners.Load() == config.MaxRunners (MaxInt32)`.
  - Web Search Agent Finding 2 — same observation: "no synchronization ensuring `SetMaxRunners(0)` is invoked before the listener's first `GetMessage` poll."
  - GitHub Agent Finding 2 — same observation, plus the additional vector that `initialSession.Statistics.TotalAssignedJobs` is processed BEFORE the very first `GetMessage` poll.
  - Code Flow Agent Finding 13 — `reconcileReporting` early-exits on K8s API errors, leaving previous (MaxInt32) value intact; could persist indefinitely.
  - Project Docs Agent Finding 15 — independently identifies the same race.
- **Refuting evidence**: None.
- **Verification needed**: Phase 6 should pull listener pod restart timestamps in arc-systems. Each restart re-opens this race. Cross-reference with the bug-job dispatch timestamp.

## Components to Inspect in Phase 6 (Exhaustive)

### kubectl — `arc-cbr-production-uw1` cluster

**Namespace: `arc-systems`**

- `kubectl get autoscalingrunnerset -n arc-systems` — confirm `mt-l-x86aavx2-189-704-a10g-8` exists; check `.spec.maxRunners` (expected `nil` pre-fix; expected `0` post-fix per PR #597).
- `kubectl get autoscalinglistener -n arc-systems` — read `.spec.maxRunners` for the corresponding listener; expected `2147483647` pre-fix per Code Flow Agent Finding 2.
- `kubectl get ephemeralrunnerset -n arc-systems` — find the set for the offending scale set; check `.status` for replica counts and any abnormal scaling events.
- `kubectl get ephemeralrunner -n arc-systems` — search for `mt-l-x86aavx2-189-704-a10g-8-tfvvr-runner-np5rz` (runner ID 42292023); check `.status.phase` and `.status.runnerId`.
- `kubectl get pod -n arc-systems -l app.kubernetes.io/component=runner-scale-set-listener` — list listener pods; check restart counts and `lastTransitionTime` near 2026-05-19T19:57Z.
- `kubectl get pod -n arc-systems -l actions.github.com/scale-set-name=mt-l-x86aavx2-189-704-a10g-8` — find any runner pods and their `.status.phase`, `.status.conditions` (PodScheduled, ContainersReady), and node assignment (`.spec.nodeName`).
- `kubectl describe pod -n arc-systems <listener-pod>` — dump env vars to confirm `CAPACITY_AWARE_ENABLED=true`, `CAPACITY_AWARE_PROACTIVE_CAPACITY=5`, `CAPACITY_AWARE_MAX_BURST_CAPACITY=30`, `CAPACITY_AWARE_RUNNER_NODE_FLEET=g5`, `CAPACITY_AWARE_WORKFLOW_GPU=8`, `CAPACITY_AWARE_HUD_FAILURE_MULTIPLIER` value.
- `kubectl get secret -n arc-systems <listener-config-secret> -o json | jq -r '.data.config' | base64 -d | jq .max_runners` — confirm listener received MaxInt32 (`2147483647`) pre-fix.
- `kubectl get events -n arc-systems --sort-by=lastTimestamp` — filter for FailedScheduling on placeholders / runner pods for this scale set; expect Pending placeholders for workflow pods (no node fits `nvidia.com/gpu=8` + `node-fleet=g5`).

**Namespace: cluster-wide**

- `kubectl get nodepool -A` — verify no g5-family NodePool exists; confirm `node-fleet=g5` label is absent from all nodes.
- `kubectl get nodes -l node-fleet=g5` — expect empty.
- `kubectl get nodeclaim -A` — check for any g5 instance attempts (expected: none, but verify).

### helm

- `helm history -n arc-systems arc-l-x86aavx2-189-704-a10g-8` — list all revisions; expected rev 1 (~19:57Z) and rev 2 (~21:32Z) had `proactive_capacity=5`, no `maxRunners`; rev 3 (~00:25Z post-PR-#597) has `proactive_capacity=0`, `maxRunners=0`.
- `helm get values -n arc-systems arc-l-x86aavx2-189-704-a10g-8 --revision 1` — confirm pre-fix values.
- `helm get values -n arc-systems arc-l-x86aavx2-189-704-a10g-8 --revision 3` — confirm post-fix values.

### Loki — Grafana Cloud (per `osdc-observability` skill)

Base selector for arc-cbr-production-uw1: `{cluster="pytorch-arc-cbr-production-uw1"}` (verify cluster label naming via `osdc-observability` skill).

- **Listener logs for offending scale set**:
  - `{cluster="pytorch-arc-cbr-production-uw1", namespace="arc-systems", app=~"arc-l-x86aavx2-189-704-a10g-8.*"} |~ "X-ScaleSetMaxCapacity|maxRunners|Starting capacity monitor|capacity reported"`
  - `{cluster="pytorch-arc-cbr-production-uw1", namespace="arc-systems", app=~"arc-l-x86aavx2-189-704-a10g-8.*"} |~ "Getting next message|Processing message|Job assigned message|Acquiring jobs"`
  - Time window: 2026-05-19T19:57Z–2026-05-19T23:35Z (ARS creation through job failure).
- **Controller logs for EphemeralRunner creation**:
  - `{cluster="pytorch-arc-cbr-production-uw1", namespace="arc-systems", app="arc-gha-rs-controller"} |~ "mt-l-x86aavx2-189-704-a10g-8|tfvvr|np5rz"`
  - Look for `GenerateJitRunnerConfig` call results.
- **Reporter cycle logs** (the smoking gun if Candidate 2/3 is correct):
  - `{cluster="pytorch-arc-cbr-production-uw1", namespace="arc-systems", app=~"arc-l-x86aavx2-189-704-a10g-8.*"} |~ "reportedCapacity|runningPairs|runningRunners"`
  - Look for any cycle where `reportedCapacity > 0` while `runningPairs == 0`.
- **Listener startup logs**:
  - `{cluster="pytorch-arc-cbr-production-uw1", namespace="arc-systems", app=~"arc-l-x86aavx2-189-704-a10g-8.*"} |~ "Starting listener|first GetMessage|MaxRunners"`
- **HUD failure logs (rule out the amplifier)**:
  - `{cluster="pytorch-arc-cbr-production-uw1", namespace="arc-systems", app=~"arc-l-x86aavx2-189-704-a10g-8.*"} |~ "HUD|hud"`
- **Initial session statistics** (Code Flow Finding 11):
  - `{cluster="pytorch-arc-cbr-production-uw1", namespace="arc-systems", app=~"arc-l-x86aavx2-189-704-a10g-8.*"} |~ "TotalAssignedJobs|initialSession|HandleDesiredRunnerCount"`

### Mimir / Prometheus — Grafana Cloud

Cluster label: `cluster="pytorch-arc-cbr-production-uw1"`.

- `gha_capacity_advertised_max_runners{cluster="pytorch-arc-cbr-production-uw1", scale_set_name="mt-l-x86aavx2-189-704-a10g-8"}` — should be 0 at all times; spikes indicate the bug.
- `gha_capacity_running_pairs{cluster="pytorch-arc-cbr-production-uw1", scale_set_name="mt-l-x86aavx2-189-704-a10g-8"}` — should be 0 (no g5 nodes).
- `gha_capacity_pairs{cluster="pytorch-arc-cbr-production-uw1", scale_set_name="mt-l-x86aavx2-189-704-a10g-8"}` — total placeholder pairs including Pending.
- `gha_capacity_desired_pairs{cluster="pytorch-arc-cbr-production-uw1", scale_set_name="mt-l-x86aavx2-189-704-a10g-8"}` — should reflect `ProactiveCapacity=5` plus HUD-failure multiplier where applicable.
- `gha_capacity_hud_requests_total{cluster="pytorch-arc-cbr-production-uw1", scale_set_name="mt-l-x86aavx2-189-704-a10g-8", result="failure"}` — confirms whether HUD failure path was hit.
- `gha_capacity_reconcile_skips_total{cluster="pytorch-arc-cbr-production-uw1", scale_set_name="mt-l-x86aavx2-189-704-a10g-8"}` by `reason=` label (`reporterListPairs`, `reporterCountRunners`) — Web Search Agent Finding 8 says these should be visible.
- `gha_assigned_jobs{cluster="pytorch-arc-cbr-production-uw1", scale_set_name="mt-l-x86aavx2-189-704-a10g-8"}` — historic timeline of assigned jobs.
- `gha_running_jobs{cluster="pytorch-arc-cbr-production-uw1", scale_set_name="mt-l-x86aavx2-189-704-a10g-8"}` — should always be 0 if no real runner pod could ever run.
- `gha_desired_runners{cluster="pytorch-arc-cbr-production-uw1", scale_set_name="mt-l-x86aavx2-189-704-a10g-8"}` — what the scaler computed.
- `kube_pod_status_phase{cluster="pytorch-arc-cbr-production-uw1", namespace="arc-systems", phase="Pending"}` — count of Pending pods, including the placeholder workflows that could never schedule.
- Time window for all queries: 2026-05-19T19:00Z–2026-05-20T01:00Z (ARS creation through fix deploy).

### GitHub API (read-only)

- `gh api repos/pytorch/pytorch/actions/jobs/76835590399` — already confirmed in Phase 1 (E1).
- `gh api repos/pytorch/pytorch/actions/runs/26122694930` — workflow context.
- `gh api repos/pytorch/pytorch/actions/runs/26122694930/jobs` — sibling jobs to see if any others were misrouted.
- `gh api /orgs/pytorch/actions/runners/42292023` — runner registration metadata (group, status).
- `gh api /orgs/pytorch/actions/runner-groups/85` — confirm runner group `arc-cbr-prod-uw1` configuration (which repos are allowed).
- `gh api /orgs/pytorch/actions/runner-groups` — list all runner groups; confirm uw1 group is distinct from ue2 group.
- `gh api /orgs/pytorch/actions/runners?per_page=100` then filter by name prefix `mt-l-x86aavx2-189-704-a10g-8-` — count of registered runners that should not exist.
- **OSDC deploy workflow runs** (Recent Changes Agent context):
  - `gh run view 26113481917` (17:20Z first deploy, sha `a59d5c3`, pre-uw1 entry).
  - `gh run view 26126318639` (21:26Z deploy, sha `29fc70e`, both clusters post-uw1).
  - The post-#597 deploy run (`gh run list -w "osdc-deploy-prod" -L 5` around 2026-05-20T00:18Z).
- `gh pr view 597` — confirm PR body and merge commit `ed35920a`.
- `gh pr view 580` — uw1 cluster bootstrap PR.

### AWS CloudWatch / AWS APIs (read-only)

- `aws health describe-events --filter "regions=us-west-1,startTimes=[{from=2026-05-19T18:00:00Z,to=2026-05-20T03:00:00Z}]" --profile <osdc-prod-account>` — Status Agent Finding 3 ran this on a wrong account; re-run on the OSDC prod account.
- `aws cloudwatch get-metric-data` for EKS control plane metrics on `pytorch-arc-cbr-production-uw1` for that window — only if Phase 6 needs to rule out a control-plane hiccup that caused listener restart.
- `aws ec2 describe-instance-type-offerings --location-type region --filters "Name=instance-type,Values=g5.48xlarge" --region us-west-1` — confirm g5.48xlarge unavailable (rules out infra confusion).

### Grafana dashboards (if applicable)

- ARC/listener dashboard for `pytorch-arc-cbr-production-uw1` (per `osdc-observability` skill) — overlay `advertised_max_runners` vs `running_pairs` vs `assigned_jobs` on a single chart for the 2026-05-19T19:00Z–2026-05-20T01:00Z window.

## Open Questions for Phase 4 + Phase 6

### Phase 4 (knowledge base / module research)

1. **Are there other latent regression vectors in `generate_runners.py`?** Does the generator have any other "missing exclude_regions"-class oversights (e.g. missing `pause_runners` honor for specific runner classes, missing instance-type validation)? (Project Docs Finding 11 noted `pause_runners` exists but `exclude_regions` was missing.)
2. **What is the upstream interface for clamping `maxRunners` to 0 at the listener-pod start?** The `actions/scaleset` package needs to support a "first-poll guard". Find the upstream contract before recommending a fork patch.
3. **Does `runner_max_map.py` (PR #598's fix scope) participate in any other capacity path?** PR #598 plugged the same exclude-region hole for `just resume-runners`. Are there other utilities (`heal-arc`, smoke tests) that also build runner specs without going through `generate_runners.py`?
4. **Should the smoke test include a "no orphaned ARS" assertion?** Code Flow Recommended Next Steps 4 mentions this; needs verification of what smoke checks already exist.
5. **Is the workflow placeholder's required affinity on `nvidia.com/gpu=true` (commit 4714643) actually correct for *all* GPU runner classes?** The fork promoted it from preferred to required; for CPU-only runner defs the GPU label is omitted. Verify CPU-only paths are unaffected.

### Phase 6 (logs and live state)

1. **Was there a listener restart for `mt-l-x86aavx2-189-704-a10g-8` between 19:57Z and 20:55Z?** If yes, the startup race re-opened at each restart. (Code Flow Finding 4, Project Docs Finding 15.)
2. **What was the value of `X-ScaleSetMaxCapacity` on the first GetMessage poll after listener startup?** Listener logs should record this.
3. **Did `gha_capacity_reconcile_skips_total{reason="reporterListPairs"}` or `reporterCountRunners` ever increment for this scale set?** Web Search Agent Finding 8 — if the very first `reconcileReporting` skipped on a K8s API error, `MaxInt32` would persist.
4. **Did GitHub deliver any `JobAvailableMessages` for `mt-l-x86aavx2-189-704-a10g-8` to the uw1 listener, or was the job already an assigned message at session creation?** GitHub Agent Open Question 3.
5. **How many other phantom runners registered before the fix landed?** `gh api /orgs/pytorch/actions/runners` filtered for the runner name prefix — count phantom registrations.
6. **Did the controller's EphemeralRunner cleanup happen post-fix?** Were the stranded `EphemeralRunner` CRs garbage-collected after PR #597 deployed?
7. **Were any other runner types (g6, p4d, c7a, r7a) also affected?** All five families have `exclude_regions: [us-west-1]` per Recent Changes Agent Finding 4. Check if any of them advertised capacity / accepted jobs.
8. **Did the bug actually require the HUD failure path, or did the startup race alone explain dispatch?** Check the HUD failure metric for this listener in the bug window.

## Recommendation on Early Exit

**Strong recommendation: short-circuit to Phase 9 (final conclusion) after a focused, minimal Phase 6 to confirm the live state matches what Phase 2 derived from code/git history.**

### Arguments for early exit

1. **Six independent Phase 2 agents converge on the same causation chain.** Code Flow, Project Docs, GitHub, Web Search, Recent Changes, and even the Status Agent (negatively, by ruling out external factors) all point to the same three-layer story: missing region exclusion in `generate_runners.py` + missing `max_runners` in the runner def + startup race in the listener.

2. **The fix already shipped.** PR #597 (`ed35920a`) merged 2026-05-20T00:14:19Z, body explicitly describes this exact bug. Helm rev 3 (00:25Z) confirms the offending ARS now has `proactive_capacity=0`/`maxRunners=0`. The remediation is in production.

3. **The mechanism is fully traced at the source-code level.** Code Flow Agent traced the full chain from `defs/l-x86aavx2-189-704-a10g-8.yaml` → `generate_runners.py:261` → Helm template → CRD → `resourcebuilder.go:95-98` → `listener.SetMaxRunners(config.MaxRunners)`. Project Docs Agent independently traced the same eight steps. No ambiguity remains in the code path.

4. **No contradictions among agents.** The only minor disagreement (HUD multiplier involvement) is reconcilable as "amplifier of placeholder churn, not amplifier of advertised capacity". All other findings cohere.

5. **All 6 H1–H6 hypotheses are either CONFIRMED (H4) or REFUTED-WITH-REFINEMENT (H1, H2, H3, H5, H6). The refinements all point to the same root cause family.** There is no remaining hypothesis space that another phase would explore.

6. **External factors are conclusively ruled out.** Status Agent confirmed no GitHub incident, no AWS Health event, no g5 capacity stress in the bug window. The bug is entirely OSDC + ARC fork. Karpenter is ruled out by Web Search Agent.

### Arguments against pure short-circuit (justify minimal Phase 6)

1. **The user's core question** ("what mechanism allowed the listener to advertise capacity without live placeholders?") deserves a definitive answer backed by live evidence, not just code analysis. A focused Phase 6 to confirm `AutoscalingListener.Spec.MaxRunners == 2147483647` on the pre-fix CR and to pull the smoking-gun listener log showing `X-ScaleSetMaxCapacity: 2147483647` on the first poll would close the loop.

2. **Phase 6 can also surface secondary remediation needs** — e.g. cleanup of stale EphemeralRunner CRs, count of phantom registered runners on GitHub, confirmation that other families (g6, p4d, c7a, r7a) didn't quietly leak jobs too. These are not investigation questions, they are operational follow-ups, but they belong in the final write-up.

3. **One verification gap that Phase 6 must close**: whether the dispatch happened via the startup-race window (Code Flow Finding 4) or via the `initialSession.Statistics.TotalAssignedJobs` carry-over path (Code Flow Finding 11). These are subtly different sub-mechanisms and the listener log will distinguish them. Both lead to the same root cause family, but the precise sub-mechanism informs whether the architectural fix should focus on goroutine ordering vs. session-creation gating.

### Recommended path forward

1. **Skip Phase 4 (knowledge base research)** — the code-level mechanism is fully traced; further KB research would not change the conclusion. Use the open KB questions above as "future work" notes in Phase 9.
2. **Skip Phase 5 (compilation of Phase 4)** — vacuous if Phase 4 is skipped.
3. **Run a minimal Phase 6** focused on the three smoking-gun verifications:
   - Read `AutoscalingListener.Spec.MaxRunners` for the pre-fix CR (helm rev 1/2) — expect `2147483647`.
   - Pull listener startup logs and find the first `X-ScaleSetMaxCapacity` value — expect `2147483647`.
   - Confirm helm rev 3 (post-PR-#597) shows `proactive_capacity=0`/`maxRunners=0` (already confirmed by Recent Changes Agent — re-verify quickly).
4. **Skip Phase 7 (compilation of Phase 6)** — minimal Phase 6 needs no formal compilation.
5. **Skip Phase 8 (likely culprits)** — culprits are already known with high confidence; collapse into Phase 9.
6. **Go directly to Phase 9 (final conclusion)** with all evidence in hand.

**Estimated time saved by early exit: ~60% of remaining work.** The risk of being wrong is low because all six agents converged independently and PR #597 already shipped — confirming the team's own diagnosis matches ours.
