# GitHub Agent — Phase 2 Findings

## Summary

The "proactive capacity" feature in the fork only updates the **listener's atomic `maxRunners`** (used to set the `X-ScaleSetMaxCapacity` HTTP header on each `GetMessage` long-poll). The **scaler's `MaxRunners` is a static value frozen at startup** and is NEVER reduced by the capacity monitor — so even if the listener advertises 0 capacity, once the GitHub Actions broker leaks a job through, the scaler will happily create up to `static_MaxRunners` EphemeralRunner pods. Combined with two known upstream behaviors — (a) the listener trusts `TotalAssignedJobs` from the broker without local validation (issue #4397), and (b) at listener startup there is a window where the listener.Run goroutine and capacity-monitor goroutine race, during which the listener advertises the full static `MaxRunners` before the first `reconcileReporting()` clamps it down — a uw1 listener for a runner type whose nodepool only exists in ue1 can legitimately advertise capacity > 0 and accept a job that will never run. Specifically: the proactive-capacity placeholders never become Running (no g5 nodepool in uw1), but advertised capacity is `runningRunners + runningPairs` and at startup `runningRunners` is the unclamped baseline.

## Findings

### Finding 1: Two different `MaxRunners` in play — capacity monitor only mutates one of them

- **Confidence**: high
- **Severity**: critical
- **Detail**: In `cmd/ghalistener/main.go` lines 107-128, `MaxRunners` from the Helm chart is passed to BOTH:
  - `listener.Config.MaxRunners` (used to set the `X-ScaleSetMaxCapacity` header on every `GetMessage`)
  - `scaler.Config.MaxRunners` (used to cap `targetRunnerCount` in `setDesiredWorkerState`)
  - The capacity monitor at line 167 receives `listener.SetMaxRunners` as its callback — `capMonitor, err := capacity.New(capConfig, k8sClient, listener.SetMaxRunners, logger, capOptions...)`. It calls **only** the listener's atomic setter at `cmd/ghalistener/capacity/monitor.go:523`: `m.setMaxRunners(capacity)`.
  - The scaler's `MaxRunners` is NEVER reduced by the capacity monitor. The scaler at `cmd/ghalistener/scaler/scaler.go:237` computes `targetRunnerCount := min(w.config.MinRunners+count, w.config.MaxRunners)` using its own static field.
  - Consequence: if the broker ever leaks a job through (because static MaxRunners was advertised during startup race or because of broker-side bugs like #4397), the scaler will create up to `static MaxRunners` real runners — there is NO defense-in-depth from the capacity monitor here.
  - File references: `/Users/jschmidt/meta/actions-runner-controller/cmd/ghalistener/main.go:107-167`; `/Users/jschmidt/meta/actions-runner-controller/cmd/ghalistener/scaler/scaler.go:228-260`; `/Users/jschmidt/meta/actions-runner-controller/cmd/ghalistener/capacity/monitor.go:482-534`.

### Finding 2: Startup race — listener advertises full static MaxRunners before first reconcileReporting

- **Confidence**: high
- **Severity**: critical
- **Detail**: At `cmd/ghalistener/main.go:180-193`, `listener.Run` and `capMonitor.Run` are launched concurrently via `errgroup`. The listener's atomic `maxRunners` is initialized to `config.MaxRunners` (the static chart value, e.g. 100 or higher) at `listener.New` → `SetMaxRunners(config.MaxRunners)` (upstream `listener.go:129`).
  - The listener immediately calls `client.GetMessage(ctx, lastMessageID, int(l.maxRunners.Load()))` (upstream `listener.go:180-184`) — sending the full static `MaxRunners` as `X-ScaleSetMaxCapacity` BEFORE the capacity monitor's first `reconcileReporting()` has run.
  - The capacity monitor's `Run` (`monitor.go:244-276`) does an initial provisioning + reporting before entering the ticker loop. But `reconcileProvisioning` itself can take seconds (HUD HTTP call + retry, K8s list calls + retry). During that window, the broker is free to assign any queued jobs matching this scale set's labels to it.
  - Even worse: the initial `handleStatistics` + `HandleDesiredRunnerCount` (upstream `listener.go:158-167`) processes `initialSession.Statistics.TotalAssignedJobs` BEFORE the very first `GetMessage` — so any jobs that the broker had pre-staged for this scale set are accepted with no capacity gate.
  - File references: `/Users/jschmidt/meta/actions-runner-controller/cmd/ghalistener/main.go:111,180-193`; `/Users/jschmidt/go/pkg/mod/github.com/actions/scaleset@v0.3.0/listener/listener.go:129,158-167,180-184`.

### Finding 3: Workflow placeholders use REQUIRED affinity on runner-class — they will stay Pending forever when no matching node exists (uw1 + g5)

- **Confidence**: high
- **Severity**: critical (specific to this bug)
- **Detail**: Commit `4714643 "Require runner-class in workflow affinity"` promoted runner-class from a `preferred` (weight 100) to a `required` node affinity term. See `/Users/jschmidt/meta/actions-runner-controller/cmd/ghalistener/capacity/placeholder.go:479-544` (`buildWorkflowAffinity`):
    ```go
    runnerClassReq = corev1.NodeSelectorRequirement{
        Key:      "osdc.io/runner-class",
        Operator: corev1.NodeSelectorOpIn,
        Values:   []string{pm.config.RunnerClass},
    }
    // ...required terms: runner-class always; GPU label when WorkflowGPU > 0.
    requiredExprs := []corev1.NodeSelectorRequirement{runnerClassReq}
    if pm.config.WorkflowGPU > 0 {
        requiredExprs = append(requiredExprs, corev1.NodeSelectorRequirement{
            Key:      "nvidia.com/gpu",
            Operator: corev1.NodeSelectorOpIn,
            Values:   []string{"true"},
        })
    }
    ```
  - For `mt-l-x86aavx2-189-704-a10g-8` in uw1: no g5 NodePool exists → no node has the required `nvidia.com/gpu=true` label → workflow placeholders stay Pending forever → `pair.BothRunning()` returns false → `runningPairs == 0`.
  - The `reconcileReporting` at `monitor.go:482-534` computes `capacity := runningRunners + runningPairs`, then `setMaxRunners(capacity)`. If both are 0 at the moment of the report tick, advertised capacity goes to 0. **Good.** BUT only AFTER the first reporter tick. And only IF runningRunners is 0 — see Finding 4.
  - File reference: `/Users/jschmidt/meta/actions-runner-controller/cmd/ghalistener/capacity/placeholder.go:479-544`.

### Finding 4: `runningRunners` counts real EphemeralRunner pods that are stuck Pending or fail — once a phantom runner is dispatched, advertised capacity stays > 0

- **Confidence**: high
- **Severity**: critical (this is the feedback loop)
- **Detail**: `countRunnersByPhaseWithRetry` in `monitor.go:196-218` counts pods with label `actions-ephemeral-runner=True,actions.github.com/scale-set-name=<name>` and groups by phase. The reporter at `monitor.go:513` uses `runningRunners := counts[corev1.PodRunning]`.
  - If a real EphemeralRunner pod has been dispatched and is in `PodRunning` (the runner agent is running, even if the workflow container has not started — "Initialize containers" is a step BEFORE container start, but the runner pod itself can be `PodRunning` because the runner-agent container started OK), then `runningRunners >= 1`. Advertised capacity becomes >= 1. Broker can dispatch another job.
  - The user's report: the runner "ran for ~2h before failing at Initialize containers". During those ~2 hours, the EphemeralRunner pod was Running (runner-agent waiting for job container init) → `runningRunners >= 1` → advertised capacity stayed > 0 → broker may dispatch additional jobs to this same listener for the same label, perpetuating the leak.
  - File reference: `/Users/jschmidt/meta/actions-runner-controller/cmd/ghalistener/capacity/monitor.go:196-218,482-534`.

### Finding 5: Listener trusts `TotalAssignedJobs` from the broker without local validation (upstream issue #4397)

- **Confidence**: high
- **Severity**: high
- **Detail**: Upstream issue [#4397](https://github.com/actions/actions-runner-controller/issues/4397) "listener: stale TotalAssignedJobs from GitHub Actions service causes permanent over-provisioning after platform incidents" — confirmed closed but unresolved on the listener side. Quote from the bug report:
    > "The listener has no local job state — `TotalAssignedJobs` flows directly from the GitHub Actions service through to the scaling calculation with no validation: `RunnerScaleSetMessage.Statistics.TotalAssignedJobs → listener.handleMessage() → handler.HandleDesiredRunnerCount(TotalAssignedJobs) → worker.setDesiredWorkerState(count) → targetRunnerCount = min(MinRunners + count, MaxRunners)`. The listener trusts `TotalAssignedJobs` completely. There's no reconciliation against `TotalRunningJobs`, no TTL on stale assignments, and no mechanism to detect that the gap between assigned and running has become permanent."
  - Workaround per the bug report: delete the AutoscalingRunnerSet CR to force a fresh registration. Restart of the listener pod does NOT clear it (state is server-side).
  - This matches the user's symptom: a stale assignment for a runner type that should never have been assigned to uw1, persisting for ~2 hours.

### Finding 6: Issue #3446 — server-side is the only gate when MaxCapacity=0

- **Confidence**: high
- **Severity**: high (architectural)
- **Detail**: Upstream issue [#3446](https://github.com/actions/actions-runner-controller/issues/3446) "scaleSetListener ignores scaling settings while acquiring the jobs". Maintainer @nikola-jokic confirmed: *"Closing this one since it is fixed on the back-end side. All versions after 0.9.2 contain the capacity information, and the back-end would not assign jobs to the scale set anymore if the capacity is 0."*
  - PR [#3431](https://github.com/actions/actions-runner-controller/pull/3431) "Propagate max capacity information to the actions back-end" added the `X-ScaleSetMaxCapacity` header.
  - **Implication**: The listener has NO local check. It will `AcquireJobs` for everything in `msg.JobAvailableMessages` (upstream `listener.go:214-218,242-257`). The only protection against over-assignment is the broker honoring the advertised capacity. **Any race in which the listener advertises >0 when it shouldn't, lets the broker through, and the listener will accept whatever it gets.**

### Finding 7: HUD label filtering is local-only (fork PR #5 open) — uw1 listener sees ue1's queued jobs

- **Confidence**: high
- **Severity**: moderate (feeds the placeholder pump but does not directly cause the leak)
- **Detail**: `cmd/ghalistener/capacity/hud_client.go:55-94` `GetQueuedJobsForLabels` calls the bare HUD endpoint and filters rows locally by `RunnerLabel` matching the scale set's labels (`monitor.go:230`). HUD does not know about regions/clusters — it aggregates queued jobs per runner label across the entire org. So a uw1 listener for `mt-l-x86aavx2-189-704-a10g-8` will see queued jobs intended for ue1 → `desiredPairs = ProactiveCapacity + queuedJobs` → placeholders created in uw1.
  - These placeholders go Pending forever (no g5 nodes in uw1) and would be cleaned up by `CleanupTimedOut` after `PlaceholderTimeout` (default 5m). But the constant re-creation churn keeps the placeholder pool full of broken pairs (then cleaned up by `CleanupBroken` in `d219a11`).
  - Fork PR [#5](https://github.com/jeanschmidt/actions-runner-controller/pull/5) by @huydhn adds server-side `runnerLabels` filter — open at investigation time, not yet merged. Even when merged, it doesn't fix the cross-region visibility (HUD still aggregates across clusters), just reduces payload size.
  - File reference: `/Users/jschmidt/meta/actions-runner-controller/cmd/ghalistener/capacity/hud_client.go:55-94`; `/Users/jschmidt/meta/actions-runner-controller/cmd/ghalistener/capacity/monitor.go:220-240,440-443`.

### Finding 8: HUD failure fallback over-provisions placeholders (`d5d94fb`)

- **Confidence**: high
- **Severity**: moderate
- **Detail**: Commit `d5d94fb "HUD failure fallback: over-provision placeholders"`: when HUD is unreachable, `desiredPairs = ProactiveCapacity * HUDFailureMultiplier` (default 3x) — see `monitor.go:440-443`. This is intentional ("less information about queue depth means we must lean toward more capacity"), but during HUD outages a uw1 listener for a wrong-region runner type will create even more wasted placeholders. Not the root cause but amplifies the symptom.
  - File reference: `/Users/jschmidt/meta/actions-runner-controller/cmd/ghalistener/capacity/monitor.go:440-443`.

### Finding 9: Initial reporter cycle doesn't gate the listener — only the loop does

- **Confidence**: high
- **Severity**: critical
- **Detail**: `monitor.go:274-276`:
    ```go
    m.reconcileProvisioning(ctx)
    m.reconcileReporting(ctx)
    ```
  These run BEFORE the reporter goroutine starts at line 286. **But** they run inside the capacity monitor goroutine, which is launched at `main.go:190-193` concurrently with the listener at lines 183-188. The listener does not wait for the first `reconcileReporting()` to complete before its first `GetMessage`. There is no synchronization between them.
  - The initial reporting WILL eventually drop `maxRunners` to 0 (since `runningPairs=0` and `runningRunners=0` at startup). But the window between listener.Run starting and reportingReporting completing is the leak window — bounded by HUD API timeout (10s, plus 3 retries with exponential backoff = up to ~17s), K8s list latency, and TCP/TLS handshake overhead.
  - File reference: `/Users/jschmidt/meta/actions-runner-controller/cmd/ghalistener/capacity/monitor.go:274-290`; `/Users/jschmidt/meta/actions-runner-controller/cmd/ghalistener/main.go:180-193`.

### Finding 10: Upstream issue #4004 confirms broker job assignment is "arbitrary" for HA scale sets

- **Confidence**: medium
- **Severity**: contextual
- **Detail**: Upstream issue [#4004](https://github.com/actions/actions-runner-controller/issues/4004) "Uneven Job Distribution Between ARC Runner Scale Sets in High Availability Setup". The documented HA pattern (per @nikola-jokic in #4283) is two scale sets with the SAME name but DIFFERENT runner groups. The broker assigns jobs "arbitrarily" between them. This means a uw1 cluster registering scale sets to a uw1-specific runner group is the correct pattern, but the broker may still leak across groups when:
  - the runner labels match (same `mt-l-x86aavx2-189-704-a10g-8` label exists in both groups), AND
  - one cluster's advertised capacity drops to 0 → broker should route to the other, BUT
  - if the wrong cluster ever briefly advertises >0 (startup race, stale runningRunners), the broker may pick it.
  - Combined with the user's setup (uw1 has `arc-cbr-prod-uw1` group; presumably ue1 has its own group), the leak likely originates from the uw1 listener temporarily advertising capacity > 0 for a label that has queued work elsewhere.

### Finding 11: Fork PR / commit timeline (relevant subset)

- **Confidence**: high
- **Severity**: documentation
- **Detail**: Relevant commits in `/Users/jschmidt/meta/actions-runner-controller` (master branch, no upstream/main remote configured):
  - `6c56d56` (Apr 23) — Initial "capacity-aware placeholder pod pre-warming". Introduces monitor, placeholders, HUD client.
  - `1944a96` (Apr 23) — Split monitor into provisioner + reporter.
  - `0fa4cb2` (Apr 23) — Add anchor ConfigMap and configurable HUD URL.
  - `8e5df74` (Apr 24) — Add defensive sleep timeout to placeholders (so pods don't leak if listener crashes).
  - `2c07a07` (Apr 28) — Split runner/workflow placeholder fleets.
  - `e3dfeae` (Apr 28) — Drop runner-class from runner placeholders (runner pool has no runner-class label).
  - `24a4afc` (Apr 30) — Add Prometheus metrics.
  - `f6c56d3` (Apr 30) — **CRITICAL**: Add MaxBurstCapacity and MaxRunners headroom. Quote from message: *"The previous MaxRunners clamp (`min(desired, MaxRunners)`) allowed up to MaxRunners placeholders ON TOP of real runners, effectively doubling the cap."* — fixes a doubling bug in placeholder count, but does NOT fix the scaler's static MaxRunners.
  - `24bf64a` (Apr 30) — Batch runner pod listing into single API call.
  - `d219a11` (May 1) — **Detect and replace broken placeholder pairs** (`CleanupBroken`). Without this, broken pairs (one pod missing) would count as healthy and capacity would silently shrink. Suggests broken pairs were observed in production.
  - `4714643` (May 5) — **Require runner-class in workflow affinity** (promoted from preferred to required). Quote from message: *"A preferred runner-class term let placeholders land on non-matching nodes where the real workflow pod (which uses a required term) could never follow — wasting the reservation. Making it required ensures placeholders only occupy nodes the actual pod can schedule onto."* Suggests previously placeholders WERE landing on wrong nodes — and this fix made them stay Pending instead, which is part of the no-running-pair problem in uw1.
  - `d5d94fb` (May 15) — HUD failure fallback: over-provision (3x by default).
  - Open: PR [#5](https://github.com/jeanschmidt/actions-runner-controller/pull/5) "ghalistener/capacity: jitter + slower HUD poll, send runnerLabels per scale set" by @huydhn.

## Open Questions

1. What is the actual Helm chart `maxRunners` value for `mt-l-x86aavx2-189-704-a10g-8` in uw1 (vs ue1)? If uw1 has `maxRunners > 0`, the scaler will permit the dispatch even if the listener advertised 0 — the static `MaxRunners` in the scaler is the hard cap. (Setting `maxRunners: 0` in uw1 for runner types that should not exist there would close most of the leak — but is not the architectural fix.)
2. What is `MinRunners` for this scale set in uw1? If `minRunners > 0`, the scaler will create runners even when no jobs are assigned (idle warm pool) — these would always be in `PodRunning` and would keep `runningRunners > 0`, making advertised capacity perpetually > 0.
3. Is the scale set in uw1 actually receiving `JobAvailableMessages` for the `mt-l-x86aavx2-189-704-a10g-8` label from the broker — or is the job arriving via `JobAssignedMessages` (already committed by the broker before the listener could refuse)? Logs would distinguish: search for the runner request ID 76835590399 in the listener pod logs.
4. Was there a listener restart in uw1 close to the time of the job assignment? Both `f6c56d3` (over-provisioning fix) and `d5d94fb` (HUD failure 3x multiplier) post-date the initial deploy — chart bumps cause restarts that re-open the startup race window.
5. Are the scale set labels in uw1 actually identical to the ue1 scale set's labels? If they differ in some `arc-cbr-prod-uw1`-prefixed group label but share the `mt-l-x86aavx2-189-704-a10g-8` label, the broker may route by label intersection only (without considering group exclusivity for capacity computation).

## Recommended Next Steps

1. **Logs (Phase 6)**: pull listener pod logs from `arc-cbr-production-uw1` for the time window of run 26122694930 / job 76835590399. Specifically look for:
   - The capacity monitor's "starting capacity monitor" log with `proactiveCapacity`, `maxRunners`, `runnerNodeFleet`, `runnerClass` values
   - "provisioning reconciled" log lines with `runningRunnerPods`, `pendingRunnerPods`, `desiredPairs`, `currentPairs`
   - "capacity reported" log lines with `runningPairs`, `runningRunners`, `reportedCapacity`
   - "Calculated target runner count" log from scaler with `assigned job`, `decision`, `max`
   - Any "Getting next message" / "Processing message" log mentioning `lastMessageID` and any `Job assigned message received` / `Acquiring jobs` for this scale set
   - Listener restart timestamps in the 24 hours before the leak
2. **Live state (Phase 6)**: query Prometheus / Mimir for the uw1 listener's `gha_capacity_advertised_max_runners`, `gha_capacity_running_pairs`, `gha_capacity_pairs`, `gha_capacity_desired_pairs`, `gha_assigned_jobs`, `gha_running_jobs`, `gha_desired_runners` over the 4-hour window before and after the assignment. Look for a moment where `gha_capacity_advertised_max_runners > 0` while `runningPairs == 0`.
3. **Architectural fix candidates** (do not implement — document for Phase 8/9):
   - Make the scaler's `MaxRunners` also dynamic via a shared atomic, so reducing capacity at the listener side ALSO clamps the scaler's hard cap.
   - Add a startup gate: don't start `listener.Run` until the first `reconcileReporting()` completes successfully. (Could be implemented with a channel or `sync.Once`.)
   - At the scaler, refuse to scale up beyond the listener's currently-advertised capacity (cross-check with `listener.maxRunners.Load()` instead of the static config field).
   - At the chart level: per-region/per-cluster `maxRunners: 0` for runner types whose backing NodePool does not exist in that region.
4. **External signal**: file an upstream issue on actions/actions-runner-controller noting that the listener's local AcquireJobs path is unconditional (issue #3446 closed as "fixed on backend") and asking for a defense-in-depth gate that also checks current advertised capacity before accepting `JobAvailableMessages`.
