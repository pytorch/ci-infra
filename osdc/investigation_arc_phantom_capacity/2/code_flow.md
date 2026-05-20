# Code Flow Agent — Phase 2 Findings

## Summary

The advertised capacity to GitHub is sent via the HTTP header `X-ScaleSetMaxCapacity` on every long-poll request from the listener. The value is `l.maxRunners.Load()` (an atomic uint32 in the upstream `scaleset/listener` package). It is initialized from `Config.MaxRunners` and dynamically updated by the fork's capacity monitor via `listener.SetMaxRunners(capacity)`. The capacity monitor's **steady-state** behavior gates capacity on `BothRunning()` placeholder pairs, which would correctly block uw1 from advertising for `l-x86aavx2-189-704-a10g-8` (workflow placeholders require GPU=8 + node-fleet=g5 tolerations, which no uw1 NodePool can satisfy).

**However, three failure modes can bypass the gate**: (1) **a hard startup race** — the listener begins polling and immediately advertises its initial `Config.MaxRunners` before the capacity monitor runs its first `reconcileReporting`; (2) **the runner def omits `max_runners`**, which propagates as `nil` in the AutoscalingRunnerSet CR and resolves to `math.MaxInt32` (~2.1 billion) in `resourcebuilder.go:95-97` — so the initial value flooded to GitHub during the startup race is `MaxInt32`; (3) **HUD failure fallback** (`d5d94fb`) multiplies `ProactiveCapacity` by 3 to derive `desiredPairs` — but this only affects placeholder *creation*, not what is advertised to GitHub.

The most likely culprit chain: `max_runners` is absent in the runner def → AutoscalingListener.Spec.MaxRunners = MaxInt32 → listener starts polling with maxRunners=MaxInt32 before capacity monitor can clamp it → GitHub assigns jobs to the listener's huge advertised capacity → controller creates EphemeralRunners with JIT-pre-registered runner IDs in GitHub → pods can't schedule (no g5 NodePool in uw1) → job sits assigned for 2h until init failure.

## Findings

### Finding 1: Capacity advertised to GitHub is `maxRunners` atomic — set from initial Config.MaxRunners then dynamically by SetMaxRunners()
- **Confidence**: high
- **Severity**: critical
- **Detail**: The capacity the listener tells GitHub is computed at exactly one place: `/Users/jschmidt/meta/actions-knowledge-base/repos/scaleset/listener/listener.go:179-183`:
  ```go
  msg, err := l.client.GetMessage(
      ctx,
      lastMessageID,
      int(l.maxRunners.Load()),
  )
  ```
  This is the value passed to `MessageSessionClient.GetMessage(ctx, lastMessageID, maxCapacity int)` at `/Users/jschmidt/meta/actions-knowledge-base/repos/scaleset/session_client.go:94`, which sets it as HTTP header `X-ScaleSetMaxCapacity` at `session_client.go:142`:
  ```go
  req.Header.Set(HeaderScaleSetMaxCapacity, strconv.Itoa(maxCapacity))
  ```
  Header constant defined at `/Users/jschmidt/meta/actions-knowledge-base/repos/scaleset/client.go:37-39`.

  `l.maxRunners` is an `atomic.Uint32` (`listener.go:86`). Set by:
  - **Initial value** in `listener.New()` at `listener.go:128`: `listener.SetMaxRunners(config.MaxRunners)` — where `config.MaxRunners` comes from `listener.Config{MaxRunners: config.MaxRunners}` set in `/Users/jschmidt/meta/actions-runner-controller/cmd/ghalistener/main.go:107-114`.
  - **Dynamic update** via `SetMaxRunners(count int)` at `listener.go:108-110`: `l.maxRunners.Store(uint32(count))`. This is the callback the fork's capacity monitor invokes — see Finding 4.

### Finding 2: When `max_runners` is absent in the runner def, listener Config.MaxRunners = math.MaxInt32 (~2.1B)
- **Confidence**: high
- **Severity**: critical
- **Detail**: The flow from the OSDC runner def to the listener's `Config.MaxRunners`:

  1. **Runner def**: `/Users/jschmidt/meta/ci-infra/osdc/modules/arc-runners/defs/l-x86aavx2-189-704-a10g-8.yaml` — file has `proactive_capacity: 5`, `max_burst_capacity: 30`, but **no `max_runners` key**.
  2. **Generator** at `/Users/jschmidt/meta/ci-infra/osdc/modules/arc-runners/scripts/python/generate_runners.py:152`: `max_runners = runner.get("max_runners")` returns `None` when absent.
  3. At `generate_runners.py:261`: `max_runners_line = f"maxRunners: {max_runners}" if max_runners is not None else ""` — when absent, the line is **empty string**.
  4. **Template** `/Users/jschmidt/meta/ci-infra/osdc/modules/arc-runners/templates/runner.yaml.tpl:5-6` — `minRunners: 0\n{{MAX_RUNNERS_LINE}}` — the substitution leaves the file with just `minRunners: 0` (no `maxRunners` key).
  5. **Helm chart** `/Users/jschmidt/meta/actions-runner-controller/charts/gha-runner-scale-set/templates/autoscalingrunnerset.yaml:146-151` — `maxRunners` is rendered **only if** the value is a number: `{{- if or (kindIs "int64" .Values.maxRunners) (kindIs "float64" .Values.maxRunners) }}`. When absent, the field is omitted from the AutoscalingRunnerSet CR.
  6. **CRD type** `/Users/jschmidt/meta/actions-runner-controller/apis/actions.github.com/v1alpha1/autoscalingrunnerset_types.go:114-116`: `MaxRunners *int json:"maxRunners,omitempty"` — pointer, so `nil` when absent.
  7. **Resource builder** `/Users/jschmidt/meta/actions-runner-controller/controllers/actions.github.com/resourcebuilder.go:95-98`:
     ```go
     effectiveMaxRunners := math.MaxInt32
     if autoscalingRunnerSet.Spec.MaxRunners != nil {
         effectiveMaxRunners = *autoscalingRunnerSet.Spec.MaxRunners
     }
     ```
     **When nil → MaxInt32 (2,147,483,647)**.
  8. The value flows into `AutoscalingListener.Spec.MaxRunners` (`autoscalinglistener_types.go:46` — `int`, NOT pointer) at `resourcebuilder.go:141`, then into the listener's `ghalistenerconfig.Config.MaxRunners` at `resourcebuilder.go:193`:
     ```go
     MaxRunners: autoscalingListener.Spec.MaxRunners,
     ```
  9. The listener pod reads this from its config secret in `/Users/jschmidt/meta/actions-runner-controller/cmd/ghalistener/config/config.go:37,49-101` and uses it as `config.MaxRunners` throughout `main.go`.

  **For `l-x86aavx2-189-704-a10g-8` specifically**: `max_runners` is missing → ARS.Spec.MaxRunners = nil → listener Config.MaxRunners = **MaxInt32**.

### Finding 3: Reporter advertises capacity = min(runningRunners + runningPairs, MaxRunners)
- **Confidence**: high
- **Severity**: critical
- **Detail**: The fork's capacity monitor `reconcileReporting()` at `/Users/jschmidt/meta/actions-runner-controller/cmd/ghalistener/capacity/monitor.go:482-534` computes what `SetMaxRunners()` will be called with:
  ```go
  // line 498-505
  runningPairs := 0
  for _, pair := range pairs {
      if pair.BothRunning() {
          runningPairs++
      }
  }
  m.recorder.SetRunningPairs(runningPairs)
  // line 507-512
  counts, err := m.countRunnersByPhaseWithRetry(ctx, reporterMaxRetries)
  if err != nil {
      m.logger.Warn("failed to count runners, keeping previous capacity", "error", err)
      ...
      return
  }
  runningRunners := counts[corev1.PodRunning]
  // line 515-523
  capacity := runningRunners + runningPairs
  if m.config.MaxRunners > 0 {
      capacity = min(capacity, m.config.MaxRunners)
  }
  m.recorder.SetAdvertisedMaxRunners(capacity)
  m.setMaxRunners(capacity)
  ```

  **Three inputs**:
  1. `runningPairs` — count of placeholder pairs where `BothRunning()` is true (`/Users/jschmidt/meta/actions-runner-controller/cmd/ghalistener/capacity/placeholder.go:46-50`):
     ```go
     func (p *PlaceholderPair) BothRunning() bool {
         return p.RunnerPod != nil && p.WorkflowPod != nil &&
             p.RunnerPod.Status.Phase == corev1.PodRunning &&
             p.WorkflowPod.Status.Phase == corev1.PodRunning
     }
     ```
     Both `Pending` pods do NOT count.
  2. `runningRunners` — count of REAL EphemeralRunner pods in PodRunning phase, matched by label `actions-ephemeral-runner=True,actions.github.com/scale-set-name=<name>` (`monitor.go:196-218`).
  3. `MaxRunners` from listener config (which is MaxInt32 for `l-x86aavx2-189-704-a10g-8` — see Finding 2). When MaxRunners=MaxInt32, the `min(capacity, MaxRunners)` clamp is effectively a no-op.

  **The reporter is the only steady-state caller of `setMaxRunners()`.** It runs every 5s (`ReportInterval` default at `config.go:85`).

### Finding 4: Startup race window — listener polls GitHub with initial Config.MaxRunners BEFORE capacity monitor first runs SetMaxRunners
- **Confidence**: high
- **Severity**: critical
- **Detail**: This is the most likely root cause of the bug. In `/Users/jschmidt/meta/actions-runner-controller/cmd/ghalistener/main.go:167`:
  ```go
  capMonitor, err := capacity.New(capConfig, k8sClient, listener.SetMaxRunners, logger, capOptions...)
  ```
  The capacity monitor is given `listener.SetMaxRunners` as a callback. But:

  - At `main.go:107-118`, `listener.New(...)` is called BEFORE the capacity monitor starts. `listener.New` calls `listener.SetMaxRunners(config.MaxRunners)` at `/Users/jschmidt/meta/actions-knowledge-base/repos/scaleset/listener/listener.go:128` — so `l.maxRunners` is **initialized to `config.MaxRunners` = MaxInt32** at construction time.
  - At `main.go:180-193`, `errgroup` spawns the listener and the capacity monitor in **parallel goroutines**:
    ```go
    g.Go(func() error {
        listnerErr := listener.Run(ctx, scaler)
        ...
    })
    g.Go(func() error {
        return capMonitor.Run(ctx)
    })
    ```
    No ordering guarantee.
  - `listener.Run()` at `listener.go:144-204` does:
    1. `initialSession := l.client.Session()` — uses cached session created during `MessageSessionClient.MessageSessionClient` (called BEFORE `run()` at main.go:82).
    2. `scaler.HandleDesiredRunnerCount(ctx, initialSession.Statistics.TotalAssignedJobs)` — scales `EphemeralRunnerSet.Spec.Replicas` to N where `targetRunnerCount = min(0 + TotalAssignedJobs, MaxRunners)`. **If GitHub had jobs queued at session creation time, this immediately requests N runner pods. Capped at MaxRunners = MaxInt32, so effectively uncapped.**
    3. Enters loop: `client.GetMessage(ctx, lastMessageID, int(l.maxRunners.Load()))` — sends `X-ScaleSetMaxCapacity: MaxInt32` to GitHub.
  - `capMonitor.Run(ctx)` at `monitor.go:244-303` does:
    1. Logs startup info.
    2. `m.placeholders.CleanupOrphans(ctx)` — a list+deleteCollection round trip.
    3. `m.reconcileProvisioning(ctx)` — HUD API call (with up to 3 retries, 1s+2s+4s backoff = up to 7s on failure), then list pods, then count runner pods, then create placeholder pairs (`adjustPairs`). **The placeholder pods take seconds to minutes to reach `Running` phase.**
    4. `m.reconcileReporting(ctx)` — only NOW does it list pairs, count running, and call `setMaxRunners(capacity)`.

  **Window of vulnerability**: from listener-pod start to first `reconcileReporting` completion, the listener advertises whatever value the AutoscalingListener.Spec.MaxRunners is — i.e. **MaxInt32** for this runner.

  Even if `reconcileReporting` reports `capacity=0` on its first cycle (no Running pairs, no Running runners), the long-poll cycle of `client.GetMessage` is long: GitHub will hold the connection open for ~50s. If GitHub assigns jobs during this poll window, the listener's `MaxRunners` change to 0 mid-flight does NOT recall the in-flight poll. The first poll already advertised MaxInt32. After that poll completes (with or without job assignment), the listener calls `GetMessage` again — and at that point would use the new (lower) maxRunners value, but the damage is done.

  **Worse**: the runner pod for the JIT-registered runner is what was scheduled to a non-existent node. The JIT registration with GitHub HAPPENS BEFORE THE POD SCHEDULES (see Finding 7).

### Finding 5: Provisioner `desiredPairs` formula + HUD failure fallback (d5d94fb)
- **Confidence**: high
- **Severity**: moderate (controls placeholder creation, not what is advertised to GitHub)
- **Detail**: `reconcileProvisioning()` at `monitor.go:334-480` decides how many placeholder pairs to create:
  ```go
  // line 440-459
  desiredPairs := m.config.ProactiveCapacity + queuedJobs
  if hudFailed {
      desiredPairs = m.config.ProactiveCapacity * m.config.HUDFailureMultiplier
  }

  // Clamp by headroom against the hard runner cap. Real runner pods (running +
  // pending) consume the cap, so the placeholder pool can only fill what's left.
  if m.config.MaxRunners > 0 {
      totalRunnerPods := runningRunnerPods + pendingRunnerPods
      headroom := max(0, m.config.MaxRunners-totalRunnerPods)
      desiredPairs = min(desiredPairs, headroom)
  }

  // Clamp burst so we don't spike the cluster
  if m.config.MaxBurstCapacity > 0 {
      desiredPairs = min(desiredPairs, m.config.MaxBurstCapacity)
  }

  desiredPairs = max(desiredPairs, 0)
  ```

  For `l-x86aavx2-189-704-a10g-8`:
  - `ProactiveCapacity = 5`, `MaxBurstCapacity = 30`, `MaxRunners = MaxInt32`.
  - HUD success path: `desiredPairs = 5 + queuedJobs`, clamped by `MaxBurstCapacity=30`.
  - HUD failure path (commit d5d94fb): `desiredPairs = 5 * 3 = 15`, clamped by `MaxBurstCapacity=30` → 15.
  - `MaxRunners` clamp is no-op because `MaxInt32 - totalRunnerPods ≈ MaxInt32`.

  **HUD fallback does NOT itself cause phantom capacity.** It only changes how many placeholders the provisioner creates. The advertised capacity to GitHub is computed in `reconcileReporting` and is gated on `BothRunning()` placeholder pairs (Finding 3). For this runner type, no placeholder pair can ever become Running in uw1 (no g5 NodePool, no other nodepool tolerates `node-fleet=g5`).

  **HOWEVER**, the d5d94fb commit message says "lean toward over-provisioning" — and on a runner where placeholders cannot schedule, the provisioner will repeatedly try to create 15 placeholder pairs that will all be Pending. The provisioner does call `CleanupTimedOut` (line 364) to remove pairs that have been Pending too long (`PlaceholderTimeout = 20m` per the template). But until they time out, they accumulate.

### Finding 6: Detect and replace broken placeholder pairs (d219a11)
- **Confidence**: high
- **Severity**: minor (correct behavior; doesn't cause phantom capacity)
- **Detail**: Commit `d219a11` introduced `CleanupBroken` at `/Users/jschmidt/meta/actions-runner-controller/cmd/ghalistener/capacity/placeholder.go:184-209`. The intent: if one of two pods in a pair was evicted, delete the orphan so the next reconcile creates a fresh full pair. Without this fix, `currentPairs` would count broken slots as healthy.

  Invocation in `reconcileProvisioning` at `monitor.go:392-405`:
  ```go
  brokenSuccess, brokenFailed, brokenSlots := m.placeholders.CleanupBroken(ctx, pairs)
  for _, slotID := range brokenSlots {
      delete(pairs, slotID)
  }
  ```

  This commit does NOT interact with `reconcileReporting` directly. The reporter only counts pairs where `BothRunning()` (placeholder.go:46-50), which already requires both pods to be non-nil AND both Running. A broken pair (one pod missing) is automatically excluded from `runningPairs`. So the reporter is safe even without CleanupBroken — CleanupBroken only ensures the *provisioner* keeps creating replacement capacity. **Not a phantom-capacity contributor.**

### Finding 7: JIT runner pre-registration: runner exists in GitHub BEFORE pod schedules (relevant to "registered but never scheduled" question)
- **Confidence**: high
- **Severity**: critical (this is the mechanism that ties phantom-capacity to a 2h hung job)
- **Detail**: The EphemeralRunner controller pre-registers each runner with GitHub using a JIT (just-in-time) config BEFORE the pod is created. From `/Users/jschmidt/meta/actions-runner-controller/controllers/actions.github.com/ephemeralrunner_controller.go:176-186`:
  ```go
  jitConfig, err := r.createRunnerJitConfig(ctx, ephemeralRunner, log)
  switch {
  case err == nil:
      jitSecret, err := r.createSecret(ctx, ephemeralRunner, jitConfig, log)
      ...
  ```
  And at line 648:
  ```go
  jitConfig, err := actionsClient.GenerateJitRunnerConfig(ctx, jitSettings, ephemeralRunner.Spec.RunnerScaleSetID)
  ```
  `GenerateJitRunnerConfig` at `/Users/jschmidt/meta/actions-knowledge-base/repos/scaleset/client.go:579-613` issues `POST /scalesets/{id}/generatejitconfig` to GitHub — which creates a runner in GitHub and returns its ID and a token.

  **So the sequence is**:
  1. Listener advertises capacity to GitHub (MaxInt32).
  2. GitHub returns `TotalAssignedJobs = N`.
  3. Scaler patches `EphemeralRunnerSet.Spec.Replicas = N`.
  4. EphemeralRunnerSet controller creates N `EphemeralRunner` CRs.
  5. EphemeralRunner controller calls `GenerateJitRunnerConfig` for each → **runner registered with GitHub at this point** (gets a runner ID, name, group).
  6. EphemeralRunner controller creates the pod with the JIT secret.
  7. Pod tries to schedule. **For `l-x86aavx2-189-704-a10g-8` in uw1, no node satisfies the runner pod's hard requirement (nvidia.com/gpu=8, c7i-runner+g5 fleets — actually let me re-check this for the runner pod).**

  Whether the pod schedules or not, the runner exists in GitHub. The job assigned to that runner will hang until the runner times out or the pod errors. The user observed a 2h hang ending in "Initialize containers" failure.

  This answers question 7 directly: **"runner registers with GitHub BEFORE the pod is scheduled to a node"** — yes, JIT registration is a controller-side API call independent of pod scheduling. A "registered but never scheduled" runner is the exact bug pattern observed.

### Finding 8: Per-listener placeholder placement — uw1 cannot schedule workflow placeholders for g5 runners
- **Confidence**: high
- **Severity**: critical (explains why placeholders should not have been Running, yet the bug occurred)
- **Detail**: For `l-x86aavx2-189-704-a10g-8`:
  - **Runner placeholder** (`/Users/jschmidt/meta/actions-runner-controller/cmd/ghalistener/capacity/placeholder.go:357-417`): node-selector `workload-type=github-runner, node-fleet=c7i-runner`. CPU=750m, Mem=512Mi. Lands on the cluster-wide c7i-runner pool. Schedules easily in uw1.
  - **Workflow placeholder** (`placeholder.go:419-471`): tolerations include `node-fleet=g5` (from `NodeFleet`), `nvidia.com/gpu` Exists. Resource requests: CPU=189, Mem=704Gi, `nvidia.com/gpu=8`. Required affinity: `osdc.io/runner-class DoesNotExist`, `nvidia.com/gpu=true`. Preferred (weight 50): `node-fleet=g5`.

  uw1 NodePools: `nodepools` (the standard pool), `nodepools-h100` (p5.48xlarge with `node-fleet=p5` label/taint, see `/Users/jschmidt/meta/ci-infra/osdc/modules/nodepools-h100/generated/p5-48xlarge.yaml:22-49`). **No g5 NodePool.** Workflow placeholder tolerates only `node-fleet=g5` — it does NOT tolerate `node-fleet=p5`. So workflow placeholders for this runner type CANNOT schedule on any uw1 node.

  **Therefore**: `BothRunning()` is permanently false → `runningPairs` is permanently 0 → `reconcileReporting` should compute `capacity = 0 + runningRunners` and `setMaxRunners(0 + runningRunners)`. With no jobs initially, capacity = 0. **Steady-state should be safe.**

  **The fact that GitHub assigned a job means the listener advertised capacity > 0 at some point.** Given Finding 4 (startup race) and Finding 2 (MaxInt32 initial value), the most plausible mechanism is the startup race window where `l.maxRunners.Load() == MaxInt32` before the first `reconcileReporting`.

### Finding 9: MaxBurstCapacity and MaxRunners headroom commit (f6c56d3 / PR #3 a3c294a)
- **Confidence**: high
- **Severity**: moderate
- **Detail**: Commit f6c56d3 (merged in PR #3 as a3c294a) added `MaxBurstCapacity` and reworked the `MaxRunners` headroom calculation. The headroom logic (Finding 5) prevents the provisioner from creating placeholders on top of real Running+Pending runner pods.

  **This commit does NOT change how max_runners is reported to GitHub.** It only changes how placeholder counts are bounded. The reporter still uses `runningRunners + runningPairs` (Finding 3). Pre-PR3 behavior: `min(desired, MaxRunners)` for `desiredPairs` (allowed up to MaxRunners placeholders ON TOP of real runners, effectively doubling the cap). Post-PR3 behavior: `min(desired, MaxRunners - (Running+Pending real runners))`.

  Question 6 answer: **PR #3 (a3c294a) does NOT change what is reported to GitHub.** It only affects placeholder provisioning. The reported capacity logic at `reconcileReporting()` lines 482-534 was NOT changed by PR #3 — only `reconcileProvisioning()` was reworked.

### Finding 10: `proactive_capacity_max_runners` branch name is misleading
- **Confidence**: medium
- **Severity**: minor
- **Detail**: PR #3 was named `proactive_capacity_max_runners`, but the merge (a3c294a) only changed the `desiredPairs` formula in the provisioner and added `MaxBurstCapacity`. The reporter (which decides what is sent to GitHub via `setMaxRunners`) was unchanged in this PR. The name suggests it might affect what is reported as `max_runners` to GitHub, but inspection of the diff confirms it does not (`git show a3c294a` shows changes only in monitor.go's `reconcileProvisioning` block).

### Finding 11: Initial `reconcileProvisioning` call ordering AFTER initial session creation
- **Confidence**: high
- **Severity**: moderate
- **Detail**: The session is created at `main.go:82` in `scalesetClient.MessageSessionClient(ctx, config.RunnerScaleSetID, hostname)`. The session response contains `initialSession.Statistics.TotalAssignedJobs` (`listener.go:147-163`). If GitHub already has assigned jobs at session creation time (e.g., listener restart after a brief outage), the very first `scaler.HandleDesiredRunnerCount` call (`listener.go:163`) will scale EphemeralRunnerSet.Spec.Replicas to `min(TotalAssignedJobs, MaxRunners) = min(N, MaxInt32) = N`.

  This is independent of `l.maxRunners.Load()` — `HandleDesiredRunnerCount` uses `w.config.MaxRunners` directly at `scaler.go:237`:
  ```go
  targetRunnerCount := min(w.config.MinRunners+count, w.config.MaxRunners)
  ```
  So even WITHOUT a GetMessage call, the listener will scale up to MaxInt32 runners if GitHub had assigned jobs at session creation. **This is a second pathway to phantom capacity — independent of the GetMessage long-poll race.**

### Finding 12: Capacity monitor goroutine startup has no synchronization barrier
- **Confidence**: high
- **Severity**: critical
- **Detail**: `main.go:180-203`:
  ```go
  g, ctx := errgroup.WithContext(ctx)
  ...
  g.Go(func() error {
      logger.Info("Starting listener")
      listnerErr := listener.Run(ctx, scaler)
      ...
  })
  g.Go(func() error {
      logger.Info("Starting capacity monitor")
      return capMonitor.Run(ctx)
  })
  ```
  No `sync.WaitGroup`, no channel, no `setMaxRunners(0)` before listener starts. **The orchestration has zero ordering guarantees**. There is no defensive code that says "before listener starts polling, set maxRunners to 0 / a safe value."

  A simple mitigation would be: in `main.go`, immediately after `capacity.New(...)`, call `listener.SetMaxRunners(0)` (or similar safe initial value) BEFORE spawning the listener goroutine. That code does not exist.

  Also: even when the capacity monitor does run, `capMonitor.Run` starts with `CleanupOrphans` (~1 round-trip) and `reconcileProvisioning` (~HUD call + listing + writing pods). This takes seconds to minutes. The listener has long since started polling.

### Finding 13: Reporter "keep previous capacity" on error makes startup race worse
- **Confidence**: high
- **Severity**: moderate
- **Detail**: `monitor.go:491-512` — if `listPairsWithRetry` or `countRunnersByPhaseWithRetry` fails in the reporter, the cycle returns early WITHOUT calling `setMaxRunners`:
  ```go
  pairs, err := m.listPairsWithRetry(ctx, reporterMaxRetries)
  if err != nil {
      m.logger.Warn("failed to list pairs, keeping previous capacity", "error", err)
      m.recorder.IncReconcileSkips(skipReasonReporterListPairs)
      return
  }
  ...
  counts, err := m.countRunnersByPhaseWithRetry(ctx, reporterMaxRetries)
  if err != nil {
      m.logger.Warn("failed to count runners, keeping previous capacity", "error", err)
      m.recorder.IncReconcileSkips(skipReasonReporterCountRunners)
      return
  }
  ```
  **Combined with Finding 4**: if the first reporter cycle (the one that's supposed to set capacity to 0 immediately at startup) fails for any reason, the "previous capacity" is the initial Config.MaxRunners = MaxInt32. The system keeps advertising MaxInt32 until a reporter cycle succeeds.

## Open Questions

1. **Was the bug triggered by listener restart?** If the cluster's arc-cbr-production-uw1 listener pod restarted around when the job was assigned (e.g., from a controller reconcile, image update, or node disruption), the startup race window applies. Phase 6 (logs and live state) should look at `kubectl get pod` and listener pod restart timestamps in arc-systems namespace.

2. **Does `RecordStatic(min, max)` ever cause GitHub to receive a fresh advertise?** At `main.go:104`, `metricsExporter.RecordStatic(config.MinRunners, config.MaxRunners)` is called before `listener.New` — but this is just a Prometheus gauge set, not a GitHub API call. (Verified at `/Users/jschmidt/meta/actions-runner-controller/cmd/ghalistener/metrics/metrics.go:642-645`.)

3. **What is `Replicas: -1` doing in `scaler.go:175`?** That's a sentinel for the initial JSON merge patch's "original" state (to ensure the patch generates a full Replicas key). Not a bug, but worth confirming the merge-patch behavior doesn't accidentally reset Replicas.

4. **Could the capacity monitor itself never have run?** If `CAPACITY_AWARE_ENABLED=false` (e.g., due to env var typo or chart misconfiguration), the listener falls through to `main.go:205-222` and never starts a capacity monitor. Then `MaxRunners` stays MaxInt32 forever. Phase 6 should `kubectl describe pod` the listener and dump env vars to confirm.

5. **Could `RunnerScaleSetByID` failure short-circuit the capacity monitor?** At `main.go:136-138`:
   ```go
   scaleSet, err := scalesetClient.GetRunnerScaleSetByID(ctx, config.RunnerScaleSetID)
   if err != nil {
       return fmt.Errorf("failed to get scale set for capacity monitor: %w", err)
   }
   ```
   If this call fails, the entire `run()` returns an error and the listener pod exits with status 1. Kubernetes restarts the pod and the race window starts over. **Each restart is a fresh phantom-capacity opportunity.**

## Recommended Next Steps

1. **Phase 6 (logs and live state)** — most important:
   - Pull listener pod restart history for `arc-l-x86aavx2-189-704-a10g-8-*` in `arc-systems` namespace on `arc-cbr-production-uw1` for the 24h window before the job assignment.
   - Dump listener pod env vars to confirm `CAPACITY_AWARE_ENABLED=true` and `CAPACITY_AWARE_*` values.
   - Search listener logs for the substring `"X-ScaleSetMaxCapacity"`, `"maxRunners"`, `"Starting capacity monitor"`, `"capacity reported"` (the `reconcileReporting` log line at monitor.go:525-529). Look for any cycle where `reportedCapacity` was nonzero unexpectedly.
   - Pull the AutoscalingListener resource for this scale set:
     ```bash
     kubectl get autoscalinglistener -n arc-systems -l ... -o yaml | grep maxRunners
     ```
     to verify whether MaxRunners on the CR is `2147483647`.
   - Pull the listener config secret to confirm `max_runners` field value:
     ```bash
     kubectl get secret -n arc-systems -l ... -o json | jq -r '.items[].data.config' | base64 -d | jq .max_runners
     ```

2. **Phase 4 (knowledge base)** — verify:
   - Whether prior OSDC commits ever set `max_runners` on this runner def.
   - Whether `force_proactive_capacity_zero` or `pause_runners` is set for prod-uw1 (already verified: no, neither is set).

3. **Phase 8 (likely culprits)** — strong candidates in priority order:
   a. **Missing `max_runners` in `defs/l-x86aavx2-189-704-a10g-8.yaml`** (Finding 2). This is a configuration bug — for a runner type with no NodePool in uw1, the def should have an explicit `max_runners: 0` (which would set MaxRunners=0 in the AutoscalingRunnerSet and the listener would refuse all jobs via the `min(0+count, 0) = 0` scaler clamp).
   b. **Startup race** (Finding 4 + Finding 12). The fork lacks a synchronization barrier between listener startup and capacity monitor's first `reconcileReporting`. Even if `max_runners=0` is set, this race exists for ANY new listener.
   c. **Listener restart triggering Finding 4 repeatedly** — if the listener pod has crash-looped or restarted, each restart is a fresh phantom-capacity opportunity (Finding 5 sub-bullet).
   d. **Initial session statistics** (Finding 11) — if GitHub had assigned jobs at session creation time (cleanup queue from a prior outage), the listener would scale to those jobs regardless of the steady-state `MaxRunners` clamp.

4. **Fix recommendations** (for Phase 9):
   - **Immediate**: add `max_runners: 0` to `l-x86aavx2-189-704-a10g-8.yaml` for the uw1 cluster (or, better, omit the entire def from uw1's generator output if the cluster has no g5 NodePool).
   - **Defense in depth**: in `main.go`, immediately after creating the capacity monitor, call `listener.SetMaxRunners(0)` before spawning the listener goroutine. This makes the startup race window safe.
   - **Architectural**: gate listener startup on the capacity monitor's first successful `reconcileReporting`. The listener should not begin polling GitHub until the capacity monitor has set a known-safe initial value.
   - **Generator-side**: in `generate_runners.py`, emit a warning (or error) when a runner def for a cluster has no matching NodePool. This requires teaching the generator about cluster nodepool inventory — a larger change.
