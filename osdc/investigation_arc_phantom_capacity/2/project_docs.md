# Project Documentation Agent — Phase 2 Findings

## Summary

The documentation cleanly explains every piece of the proactive-capacity
machinery — placeholder pairs, MaxRunners gating, HUD failure fallback,
maxRunners reporting via `X-ScaleSetMaxCapacity` — but it ALSO explicitly
flags the architectural hole that the bug rides on: **`generate_runners.py`
does not honor `exclude_regions`, while `generate_nodepools.py` does.** The
runner def `l-x86aavx2-189-704-a10g-8` (g5.48xlarge) is therefore generated
for every cluster, regardless of region, and is even called out for
us-west-1 in two separate docs (load-test doc, runner naming convention).
On top of that, this runner def DOES NOT SET `max_runners` — the doc
explicitly states "Without max_runners, the value is empty/unlimited" — so
GitHub can advertise arbitrary capacity for it. Combined with the HUD-failure
fallback (`ProactiveCapacity * HUDFailureMultiplier`, default 3) that does
NOT gate on placeholder liveness, the listener can hand GitHub a
non-zero `X-ScaleSetMaxCapacity` even with zero healthy placeholders.

## Findings

### Finding 1: `generate_runners.py` does NOT honor `exclude_regions` (root cause of overdeployment)

- **Confidence**: high
- **Severity**: critical
- **Detail**: The runner generator at
  `/Users/jschmidt/meta/ci-infra/osdc/modules/arc-runners/scripts/python/generate_runners.py`
  has NO region filtering, NO `exclude_regions` handling, and NO check for
  whether the underlying NodePool fleet is actually present in the target
  region. It iterates every YAML file in `defs/` and renders an
  AutoscalingRunnerSet for each, unconditionally
  (`generate_runners.py:375-377`):

  ```python
  for def_file in def_files:
      if generate_runner(def_file, template_content, cluster_config, output_dir, module_name):
          count += 1
  ```

  Contrast with the nodepool generator at
  `/Users/jschmidt/meta/ci-infra/osdc/modules/nodepools/scripts/python/generate_nodepools.py:491-500`
  which DOES check region:

  ```python
  def _is_excluded_for_region(fleet_or_pool_def, region):
      """Return True if the given region appears in the def's ``exclude_regions`` list.
      ...
      """
      if not region:
          return False
      return region in (fleet_or_pool_def.get("exclude_regions") or [])
  ```

  And later at `generate_nodepools.py:508-510`:
  ```python
  if _is_excluded_for_region(fleet_data, region):
      log_info(f"  Fleet '{fleet_name}': skipped (excluded in region '{region}')")
      return 0
  ```

  Net effect: in `arc-cbr-production-uw1`, the runner generator emits an
  `AutoscalingRunnerSet` for `l-x86aavx2-189-704-a10g-8` (g5.48xlarge),
  even though `modules/nodepools/defs/g5.yaml:8-9` excludes us-west-1
  and therefore NO g5 NodePool exists in that cluster.

### Finding 2: The runner def itself does NOT set `max_runners` — explicitly documented as unlimited

- **Confidence**: high
- **Severity**: critical
- **Detail**: `modules/arc-runners/defs/l-x86aavx2-189-704-a10g-8.yaml`
  has `proactive_capacity: 5` and `max_burst_capacity: 30` but NO
  `max_runners`. `docs/arc-fork-build-deploy.md:213` explicitly documents
  the consequence:
  > "This flows through the template as `maxRunners:` in the generated
  > Helm values (the chart's standard scaling field — `gha_max_runners` at
  > `runner.yaml.tpl:73` is an unrelated Prometheus metric). The capacity
  > monitor reads `config.MaxRunners` (set from the listener config) and
  > uses it as the ceiling for `X-ScaleSetMaxCapacity`. **Without
  > `max_runners`, the value is empty/unlimited.**"

  And one line further down, `docs/arc-fork-build-deploy.md:220`:
  > "CPU/T4/A10G/L4/A100 runner defs currently do not set `max_runners`."

  The g5.48xlarge runner def (8x A10G) falls into the A10G class and
  therefore has no `max_runners` — the listener has no upper bound to
  clamp `X-ScaleSetMaxCapacity` against.

### Finding 3: HUD failure fallback advertises capacity without placeholder-liveness gate

- **Confidence**: high
- **Severity**: critical
- **Detail**: `docs/arc-fork-build-deploy.md:179` documents the fallback
  knob `CAPACITY_AWARE_HUD_FAILURE_MULTIPLIER`:
  > "When the HUD API is unreachable, the capacity monitor over-provisions
  > placeholders to `ProactiveCapacity * multiplier`. Outer caps
  > (`MaxRunners` headroom, `MaxBurstCapacity`) bound the absolute blast
  > radius. Clamped to a minimum of `1`."

  In the OSDC template at
  `modules/arc-runners/templates/runner.yaml.tpl:166-169`, the multiplier
  is left at the code default (3). Together with Finding 2 (no
  `max_runners`, so the "outer cap" disappears), an HUD outage could
  lead to `ProactiveCapacity(5) * 3 = 15` placeholder pairs being
  requested — but since the nodes can never come up, the pairs never
  reach Running, yet `desiredPairs` is still positive.

  The fallback path is implemented in
  `cmd/ghalistener/capacity/monitor.go:441-443`:
  ```go
  desiredPairs := m.config.ProactiveCapacity + queuedJobs
  if hudFailed {
      desiredPairs = m.config.ProactiveCapacity * m.config.HUDFailureMultiplier
  }
  ```
  with the headroom clamp at lines 449-453 being a no-op when
  `MaxRunners == 0` (Finding 2).

### Finding 4: Reporter advertises capacity ONLY from real running runners + running placeholder pairs — but `MaxRunners == 0` removes the ceiling

- **Confidence**: high
- **Severity**: critical
- **Detail**: `cmd/ghalistener/capacity/monitor.go:482-534` implements
  the `reconcileReporting` function which sends `setMaxRunners` to the
  listener via `listener.SetMaxRunners`, which in turn drives the
  `X-ScaleSetMaxCapacity` header. The actual computation:

  ```go
  capacity := runningRunners + runningPairs
  if m.config.MaxRunners > 0 {
      capacity = min(capacity, m.config.MaxRunners)
  }
  ...
  m.setMaxRunners(capacity)
  ```

  At a glance this is safe — no running pairs → no advertised capacity.
  BUT, two attack vectors:
  1. **`runningRunners > 0`**: a real ephemeral runner that registered
     (via JIT config — see Finding 7) but whose pod has not yet
     scheduled may be counted as `runningRunners` once its Pod is
     `Running`. The doc never asserts the pod must be `Ready`, just
     `Running`. (See `monitor.go:506-513` which only filters by phase.)
  2. **Initial period before reporter runs**: between listener startup
     and the first reporter cycle, `l.maxRunners` is initialized from
     `config.MaxRunners` (see `repos/scaleset/listener/listener.go:128`:
     `listener.SetMaxRunners(config.MaxRunners)`). Per Finding 2,
     `config.MaxRunners == 0` here, but the listener uses
     `uint32` for the max — `0` to GitHub is effectively `0 advertised`.
     **However**: documentation at `docs/arc-fork-build-deploy.md:213`
     says "Without max_runners, the value is empty/unlimited", which
     might mean the chart omits the field entirely. If omitted, the
     controller's `resourcebuilder.go:95-96` sets
     `effectiveMaxRunners := math.MaxInt32`, which then propagates to
     the listener and finally to `X-ScaleSetMaxCapacity`. **This is a
     huge advertisement.**

### Finding 5: The proactive-capacity feature explicitly assumes `max_runners` is set, but doesn't enforce it

- **Confidence**: high
- **Severity**: critical
- **Detail**: The docs and the code both make `MaxRunners` the headroom
  clamp that prevents over-advertisement, but neither requires a runner
  def to set it. Counter-evidence — H100 and B200 defs DO set
  `max_runners` (sized as `reserved GPUs / GPUs per runner`):
  - `arc-runners-h100/defs/l-bx86iamx-176-1800-h100-8.yaml`:
    `max_runners: 1` (per Read at line 11)
  - `docs/arc-fork-build-deploy.md:215-218`:
    > "H100 (`modules/arc-runners-h100/defs/`) and B200
    > (`modules/arc-runners-b200/defs/`) runners all set `max_runners`,
    > computed as `(reserved GPUs / GPUs per runner)`."

  But the doc immediately admits the gap (line 220):
  > "CPU/T4/A10G/L4/A100 runner defs currently do not set
  > `max_runners`."

  This is precisely the runner family that hit the bug
  (`mt-l-x86aavx2-189-704-a10g-8` is 8× A10G).

### Finding 6: us-west-1 region exclusion is documented but only at the NodePool layer

- **Confidence**: high
- **Severity**: critical
- **Detail**: `docs/arc-fork-build-deploy.md:238` notes the GPU exclusion
  for us-west-1 in the load-test context (which uses arc-staging in
  us-west-1):
  > "**GPU labels in us-west-1**: g5 (A10G) and g6 (L4) fleets have
  > `exclude_regions: [us-west-1]`. Only g4dn (T4) is available — pick
  > from `l-x86iavx512-29-115-t4` (1×T4), `l-x86iavx512-45-172-t4-4`
  > (4×T4), or `l-bx86iavx512-94-344-t4-8` (8×T4, bare-metal)."

  And `docs/prod-cluster-ha-us-west-1.md:39`:
  > "**us-west-1 service-quota raises** for the vCPU families this
  > cluster uses (c7i, m7i, r7i, m7g, m8g, g4dn, g5, g6, p5). File
  > early — GPU families can have long lead times."
  (note: g5 IS listed for service quota raises — implying intent to use
  g5 in uw1 — yet `g5.yaml:8-9` excludes it. Possible drift between
  the doc and the def. But that's tangential — the bug is regardless.)

  Several runner defs explicitly comment on the us-west-1 hole at the
  arc-runners layer too — these are the c7a-based defs:
  - `modules/arc-runners/defs/l-x86iavx512-37-68.yaml:3`:
    `# NOTE: c7a.48xlarge is NOT available in us-west-1 (arc-staging). Will not schedule there.`
  - Same comment in `l-x86iavx512-8-16.yaml`, `l-x86iavx512-46-85.yaml`,
    `l-x86iavx512-16-32.yaml`, `l-x86iavx512-94-192.yaml`,
    `l-x86iavx512-2-4.yaml`.

  Translation: **the team is fully aware that runner scale sets will
  exist in clusters whose NodePools cannot back them, and chose to
  accept the situation rather than gate at the generator.** No
  equivalent comment on `l-x86aavx2-189-704-a10g-8.yaml` — but the
  same architectural shape applies (g5 also excluded in us-west-1).

### Finding 7: `docs/prod-cluster-ha-us-west-1.md` design doc explicitly assumes a "proactive_capacity_multiplier" knob — NOT yet implemented

- **Confidence**: high
- **Severity**: moderate
- **Detail**: `docs/prod-cluster-ha-us-west-1.md:126-134` ("Capacity
  ramp" section):
  > "Recommended go-live posture: start us-west-1 at **30%** of
  > us-east-2's proactive capacity for the first week to limit blast
  > radius, then ramp to 50/50 once routing looks healthy. Implement
  > via a per-cluster multiplier
  > (`clusters.<id>.arc-runners.proactive_capacity_multiplier`) so the
  > ramp is one number to change, not a per-def edit."
  >
  > "us-west-1 quotas and reservations must be sized to absorb **100%**
  > of prod traffic in the failover case, not just steady-state share."

  This `proactive_capacity_multiplier` is a planned knob, NOT in the
  current `generate_runners.py` (I verified — the only proactive cap
  manipulation is `force_proactive_capacity_zero` for staging clusters,
  applied at line 155-156). So the us-west-1 cluster currently runs
  with the same `proactive_capacity` values as us-east-2 — meaning
  ProactiveCapacity=5 placeholder pairs are attempted for the g5
  scale set on first listener startup, against zero possible nodes.

### Finding 8: clusters.yaml has a known-pending TODO acknowledging the generator gap

- **Confidence**: high
- **Severity**: moderate
- **Detail**: `clusters.yaml:213-214` (in the
  `arc-cbr-production-uw1.nodepools-h100` section):
  > "Generator change to honor this override is pending — see
  > docs/prod-cluster-ha-us-west-1.md "Phase 1 prerequisite 2"."

  This refers to a different override (capacity_reservation_ids for
  nodepools-h100) but the broader pattern — generators are aware of
  per-cluster context only partially — is the same shape as the
  arc-runners gap.

### Finding 9: Listener initial MaxRunners is sourced from the chart's `maxRunners:` field, which is empty for A10G

- **Confidence**: high
- **Severity**: critical
- **Detail**: Flow chain:
  1. `templates/runner.yaml.tpl:6`: `{{MAX_RUNNERS_LINE}}` — emits
     `maxRunners: <N>` only when `max_runners` is set in the def, per
     `generate_runners.py:261`: `max_runners_line = f"maxRunners: {max_runners}" if max_runners is not None else ""`.
  2. For the bug's runner, that line is empty.
  3. Helm renders the `AutoscalingRunnerSet` CR without
     `spec.maxRunners`.
  4. `controllers/actions.github.com/resourcebuilder.go:95-97`:
     ```go
     effectiveMaxRunners := math.MaxInt32
     if autoscalingRunnerSet.Spec.MaxRunners != nil {
         effectiveMaxRunners = *autoscalingRunnerSet.Spec.MaxRunners
     }
     ```
     So the controller substitutes `math.MaxInt32` (≈2.1B) when the
     field is nil.
  5. `resourcebuilder.go:141` and `:193` pass that `effectiveMaxRunners`
     into the `AutoscalingListener.Spec.MaxRunners` (int) and then into
     the listener config JSON.
  6. `cmd/ghalistener/main.go:111`: `MaxRunners: config.MaxRunners`
     into `listener.Config`.
  7. `repos/scaleset/listener/listener.go:128`:
     `listener.SetMaxRunners(config.MaxRunners)` — initial capacity is
     `math.MaxInt32`.
  8. `cmd/ghalistener/main.go:145`: `capConfig.MaxRunners =
     config.MaxRunners` — capacity monitor inherits the same value.

  **Until the capacity monitor's reporter runs (every 5s), the listener
  is advertising `math.MaxInt32` to GitHub via `X-ScaleSetMaxCapacity`.**
  GitHub will happily assign jobs against that ceiling — and even after
  the reporter clamps to `runningRunners + runningPairs`, the clamp
  itself is bounded only by `MaxRunners > 0` (line 517) — which is
  `MaxInt32 > 0` so it's a no-op.

  But more importantly: the very FIRST poll cycle, before any reconcile,
  has the runaway `MaxInt32` value. That window is enough for GitHub to
  match a job to the scale set.

### Finding 10: `docs/arc-fork-build-deploy.md` documents that ARC is "capacity-aware" but does NOT document the failure mode when no nodes exist

- **Confidence**: high
- **Severity**: moderate
- **Detail**: `docs/arc-fork-build-deploy.md:7-8`:
  > "**Why**: Adds capacity-aware autoscaling (proactive capacity) to
  > the `ghalistener` binary. Stock ARC is count-based and
  > capacity-unaware -- it scales runners without checking whether the
  > cluster can actually fit the runner + workflow pod pair. The fork
  > adds a CapacityMonitor goroutine that dynamically adjusts
  > `maxRunners` reported to GitHub via the `X-ScaleSetMaxCapacity`
  > header, backed by placeholder pod reservations."

  The doc claims capacity-awareness but never addresses the case where
  the runner scale set exists in a cluster whose NodePools cannot back
  it. The whole design implicitly assumes that any ARS that's been
  generated CAN, in principle, find capacity if it waits.

### Finding 11: The `pause_runners` cluster-level flag overrides max_runners to 0 — proof that the codebase already has a "pause" mechanism, just not a "region-skip" one

- **Confidence**: high
- **Severity**: minor
- **Detail**: `generate_runners.py:166-167`:
  ```python
  if cluster_config.get("pause_runners"):
      max_runners = 0
  ```
  And `clusters.yaml` defaults: `pause_runners` not set by default.
  This proves the team knows how to surgically suppress scale sets in
  specific clusters via the generator — but did not extend the same
  pattern to per-runner-def `exclude_regions`. A trivial extension
  would have been: read the runner def's referenced fleet name, look up
  `nodepools/defs/<fleet>.yaml` `exclude_regions`, and skip if matched.

### Finding 12: Documentation states proactive capacity is forced to 0 ONLY for staging clusters

- **Confidence**: high
- **Severity**: moderate
- **Detail**: `docs/arc-fork-build-deploy.md:184`:
  > "Currently enabled for all runners (`CAPACITY_AWARE_ENABLED=true`
  > is hardcoded in the template). Note: `generate_runners.py` forces
  > `proactive_capacity` to `0` for staging clusters
  > (`force_proactive_capacity_zero` is set when the cluster id
  > contains `staging`), so placeholders are not pre-provisioned in
  > staging — only on-demand pairs created for in-flight jobs."

  And `generate_runners.py:155-156`:
  ```python
  if cluster_config.get("force_proactive_capacity_zero"):
      proactive_capacity = 0
  ```
  Set at line 353: `cluster_config["force_proactive_capacity_zero"] = "staging" in cluster_id`.

  **`arc-cbr-production-uw1` does NOT contain "staging".** Therefore
  proactive capacity is enabled at the listener layer for the g5
  runner def with `proactive_capacity: 5`, which immediately tries to
  create 5 placeholder pairs.

### Finding 13: `runner_naming_convention.md` documents the g5 family for us-east-2 only — but the def has no such constraint

- **Confidence**: medium
- **Severity**: minor
- **Detail**: `docs/runner_naming_convention.md` mentions the g5 family
  via:
  ```
  ### x86 GPU — A10G (g5 family)
  | linux.g5.4xlarge.nvidia.gpu | g5.4xlarge | mt-l-x86aavx2-29-113-a10g |
  | linux.g5.12xlarge.nvidia.gpu | g5.12xlarge | mt-l-x86aavx2-45-167-a10g-4 |
  | linux.g5.48xlarge.nvidia.gpu | g5.48xlarge | mt-l-x86aavx2-189-704-a10g-8 |
  ```

  The doc treats this as a global naming convention with no per-region
  caveat. The runner defs (which inherit those names) likewise have no
  region annotation — neither in `defs/*.yaml` nor in
  `arc-runners/scripts/python/generate_runners.py`.

### Finding 14: Placeholder pods are created with hard nodeSelector — they stay Pending forever if no matching node, but never gate the advertisement

- **Confidence**: high
- **Severity**: critical
- **Detail**: From `cmd/ghalistener/capacity/placeholder.go`, the runner
  placeholder (line 372-378):
  ```go
  nodeSelector := map[string]string{
      "workload-type": "github-runner",
  }
  if pm.config.RunnerNodeFleet != "" {
      nodeSelector["node-fleet"] = pm.config.RunnerNodeFleet
  }
  ```
  And the workflow placeholder (line 484-495): required nodeAffinity
  with `osdc.io/runner-class` (DoesNotExist when unset) and the GPU
  label `nvidia.com/gpu=true` when WorkflowGPU > 0. These are hard
  requirements.

  For a g5 placeholder on uw1: the workflow placeholder has
  `nvidia.com/gpu=true` REQUIRED — no g5 node exists, so it stays
  Pending. The runner placeholder targets `c7i-runner` which DOES
  exist in uw1 — but the workflow side stays Pending forever.

  **The capacity monitor does NOT advertise capacity for non-running
  placeholders** (see Finding 4: `runningPairs` only counts both-running
  pairs). But there are two leakage points:
  1. The TIMEOUT path (`docs/arc-fork-build-deploy.md:170` —
     `PLACEHOLDER_TIMEOUT: 20m`) just DELETES timed-out placeholders
     and replaces them next cycle — does NOT mark the runner as
     unhealthy.
  2. The HUD fallback (`HUDFailureMultiplier`) creates MORE placeholders
     when HUD is unreachable, but doesn't advertise capacity.

  The bug must therefore come from EITHER: (a) the math.MaxInt32 window
  before the first reporter cycle (Finding 9), OR (b) the controller
  scaler/EphemeralRunnerSet creating real EphemeralRunners for the ARS
  whenever `TotalAssignedJobs > 0`, which then registers with GitHub
  before its pod schedules. Real registered runners are counted by
  Finding 4's `countRunnersByPhaseWithRetry` only when their Pod is
  Running — but registration with GitHub happens via JIT config which
  is independent of pod phase. **The scaler creates the EphemeralRunner
  CR; the EphemeralRunner CR controller creates the pod; the pod waits
  for a node; meanwhile a JIT config was already minted and a registered
  runner exists on GitHub side.**

### Finding 15: Critical observation — the listener's initial MaxRunners flows through `listener.SetMaxRunners(config.MaxRunners)` BEFORE any reconcile

- **Confidence**: high
- **Severity**: critical
- **Detail**: From the scaleset listener at
  `actions-knowledge-base/repos/scaleset/listener/listener.go:128`:
  ```go
  listener.SetMaxRunners(config.MaxRunners)
  ```
  This is the listener's atomic `uint32` `maxRunners`. The `Run` loop
  uses `int(l.maxRunners.Load())` as the `maxCapacity` argument to
  `GetMessage`. Per the scaleset doc
  (`actions-knowledge-base/repos/scaleset/README.md:49`):
  > "When polling for messages, include your scale set's maximum
  > capacity via the `maxCapacity` parameter (sent as the
  > `X-ScaleSetMaxCapacity` header). This allows the backend to assign
  > jobs accurately and avoid creating backlogs your scale set cannot
  > fulfill."

  So **EVERY message poll sends `X-ScaleSetMaxCapacity = MaxRunners`**
  (initially `math.MaxInt32` per Finding 9, until the capacity monitor
  reporter overwrites it 5s later). GitHub treats this as the cap on
  assignments. With `MaxInt32`, GitHub will happily assign ANY pending
  jobs labeled `mt-l-x86aavx2-189-704-a10g-8` to this scale set.

### Finding 16: Listener's MaxRunners min check is unsigned — `maxCapacity` clamps to `uint32` which makes negative impossible but also large numbers safe

- **Confidence**: medium
- **Severity**: minor
- **Detail**: `repos/scaleset/listener/listener.go:108-110`:
  ```go
  func (l *Listener) SetMaxRunners(count int) {
      l.maxRunners.Store(uint32(count))
  }
  ```
  Negative count from the capacity monitor would underflow to a huge
  uint32. But the capacity monitor clamps to `max(0, ...)` so this is
  not the bug — just an architectural concern flagged for future
  hardening.

## Open Questions

1. **Did the user's job land via the math.MaxInt32 initial advertisement
   window, or via the controller's standard EphemeralRunnerSet scaling
   path on receiving `TotalAssignedJobs > 0`?** Either way the
   architectural assumption is broken. Phase 6 (logs) should show
   listener startup events and the first GetMessage poll's
   X-ScaleSetMaxCapacity value.

2. **Why does `g5.yaml` exclude us-west-1 if quota raises are documented
   for g5 in us-west-1 (`prod-cluster-ha-us-west-1.md:39`)?** Possible
   drift between the doc and the def — needs a separate cleanup.

3. **Has the team considered a generator-level fix where
   `generate_runners.py` reads the matching `nodepools/defs/<fleet>.yaml`
   and skips the runner def when the fleet excludes the cluster's
   region?** No doc mentions this pattern — but Finding 11 shows the
   precedent exists (`pause_runners`).

4. **Is `force_proactive_capacity_zero` supposed to apply to
   `arc-cbr-production-uw1` too?** Phase 7 `prod-cluster-ha-us-west-1.md`
   suggests a `proactive_capacity_multiplier` ramp at 30% — but no such
   knob is wired today. Setting `force_proactive_capacity_zero` for the
   new uw1 cluster during initial ramp could have prevented this bug
   surfacing while the runner-def gap is fixed.

## Recommended Next Steps

1. **Phase 6 must verify**: the listener pod for
   `mt-l-x86aavx2-189-704-a10g-8` exists in `arc-systems` on the uw1
   cluster, and check its `X-ScaleSetMaxCapacity` over time via the
   `gha_capacity_advertised_max_runners` Mimir metric. Also check the
   controller-emitted `AutoscalingListener.Spec.MaxRunners` for that
   scale set — if it's `math.MaxInt32` (`2147483647`), Finding 9 is
   confirmed.

2. **Phase 6 should also check**: for the EphemeralRunner CR named
   `mt-l-x86aavx2-189-704-a10g-8-tfvvr-runner-np5rz` — was its Pod ever
   created? What was the Pod status timeline? Was the JIT registration
   completed?

3. **Phase 8 (likely culprits)**: the doc trail points strongly at the
   combination of (a) generator does not honor `exclude_regions` for
   runner defs (the upstream cause), (b) runner def has no
   `max_runners` (so the HUD-failure fallback and the initial MaxInt32
   advertisement are both unbounded), and (c) `proactive_capacity: 5`
   means the listener WILL try to create placeholders immediately on
   startup, requesting GitHub's broker to advertise capacity even before
   the placeholders are healthy.

4. **Fix doc gap**: `docs/arc-fork-build-deploy.md` should explicitly
   call out that runner defs without `max_runners` get
   `effectiveMaxRunners = math.MaxInt32` from the controller, NOT
   "empty/unlimited" — the current language understates the risk.
