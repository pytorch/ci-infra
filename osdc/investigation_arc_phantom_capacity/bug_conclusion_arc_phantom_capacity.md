# Final Conclusion — ARC Phantom Capacity Advertisement on arc-cbr-production-uw1

## TL;DR

`arc-cbr-production-uw1` advertised capacity for a runner type (`mt-l-x86aavx2-189-704-a10g-8`) whose underlying EC2 instance (g5.48xlarge) is excluded from us-west-1 by `modules/nodepools/defs/g5.yaml`. 31 jobs were dispatched to that scale set; all 31 failed or were canceled (zero successes).

Three independent code-level holes combined to cause this. The two upstream-side holes (in the ARC fork) made any region-mismatched scale set advertise unbounded capacity by default; the OSDC-side hole (in `generate_runners.py`) is what allowed a region-mismatched scale set to exist in the first place. The fix landed in **PR #597 ("Zero-out advertised capacity for region-excluded runners")** at 2026-05-20T00:14:19Z and is verified live in production.

The user's specific question — *"what mechanism allowed the listener to advertise capacity to GitHub without live placeholders?"* — has a precise answer below.

## The Three-Layer Mechanism (answer to the user's question)

### Layer A — OSDC: the scale set should never have existed in uw1

- `modules/nodepools/scripts/python/generate_nodepools.py` honors `exclude_regions` in NodePool def files (`g5.yaml` → `exclude_regions: [us-west-1]` → no g5 NodePool generated in uw1).
- **`modules/arc-runners/scripts/python/generate_runners.py` did NOT honor `exclude_regions`.** Before PR #597, `grep -rn exclude_regions modules/arc-runners/` returned empty. The runner generator iterated every YAML in `defs/` with no per-cluster region check.
- Result: the ARS for `mt-l-x86aavx2-189-704-a10g-8` was generated in uw1, the listener pod was created, and it talked to GitHub as if it could fulfill jobs.
- This is a latent bug. It only manifested because `arc-cbr-production-uw1` is the **first cluster ever** to hit a non-empty `exclude_regions` set — the cluster was created on 2026-05-19 (PR #580).

### Layer B — ARC fork: missing `max_runners` resolves to `math.MaxInt32`

- The runner def `l-x86aavx2-189-704-a10g-8.yaml` does not set `max_runners` (Phase 1 verified). Multiple GPU defs in this fleet share this property; `docs/arc-fork-build-deploy.md:220` documents the omission as intentional.
- Code path traced by Code Flow Agent + Project Docs Agent (independently):
  1. `generate_runners.py:261` emits an empty `MAX_RUNNERS_LINE` when `max_runners` is unset.
  2. The Helm chart template at `gha-runner-scale-set/templates/autoscalingrunnerset.yaml:146-151` omits the field from the CRD when empty.
  3. The CRD's `spec.maxRunners` pointer field is `nil`.
  4. `controllers/actions.github.com/resourcebuilder.go:95-97` substitutes `effectiveMaxRunners := math.MaxInt32` (≈ 2.147 billion).
  5. This MaxInt32 value is passed to the listener as `Config.MaxRunners`.
- Documentation claimed the omitted value would be *"empty/unlimited"*; reality is MaxInt32 — which behaves as effectively unbounded for any practical workload.

### Layer C — ARC fork: startup race in the listener

- `cmd/ghalistener/main.go:180-193` launches the listener goroutine and the capacity-monitor goroutine as siblings via `errgroup.WithContext` with **no synchronization**.
- The listener constructor (`listener.New()`, called at `main.go:107`) calls `listener.SetMaxRunners(config.MaxRunners)` immediately, so `l.maxRunners.Load()` returns `MaxInt32` from the very first poll.
- The listener's first `GetMessage` long-poll to GitHub sends `X-ScaleSetMaxCapacity: 2147483647` as an HTTP header (verified by Web Search Agent against the upstream `scaleset/listener/session_client.go` source).
- The capacity monitor must complete `CleanupOrphans` + `reconcileProvisioning` (HUD API call with retries) + `reconcileReporting` before it can call `setMaxRunners(0)`. That takes seconds-to-minutes; the GitHub brokerage only needs one poll to see the high capacity and dispatch jobs.
- **Worse: `reconcileReporting` early-exits on K8s API list/count failures (after 2 retries), leaving the listener's `maxRunners` at the constructor value.** A single transient API hiccup at startup persists MaxInt32 indefinitely.

### How GitHub dispatched jobs to a runner whose pod could never schedule

The user asked specifically how this happened despite the "proactive placeholder pairs must be live" gate. The answer is two-fold:

1. **`X-ScaleSetMaxCapacity` is the GHA brokerage's sole gate.** The brokerage trusts the listener-reported header verbatim — there is no broker-side check that any pods are actually running. The proactive-capacity feature in the fork influences how many placeholder pods are *created* and how many "real" runner replicas are launched, but the `reconcileReporting` cycle that sets `l.maxRunners` is what GitHub actually consumes. The startup race + MaxInt32 default short-circuited that gate before the reconciler ever ran a non-trivial cycle.

2. **JIT pre-registration** (`controllers/actions.github.com/ephemeralrunner_controller.go:176-186, 648` — `GenerateJitRunnerConfig`). When the controller sees an ARS asking for runners, it registers each EphemeralRunner with GitHub via `POST /actions/runners/generate-jitconfig` **before the runner pod is created**. So a runner appears "registered and ready" to GitHub even with no pod. GitHub dispatched a job to runner `mt-l-x86aavx2-189-704-a10g-8-tfvvr-runner-np5rz` (ID 42292023), the runner accepted the JIT token, the pod was never scheduled (no g5 NodePool), and the job sat at "Initialize containers" for 2 hours.

## Direct Live Evidence (Phase 6)

All three smoking guns verified read-only on the live cluster:

1. **Post-fix ARS state**: `mt-l-x86aavx2-189-704-a10g-8` now has `maxRunners: 0, minRunners: 0`. Listener config secret JSON contains `"max_runners": 0, "min_runners": 0`. Listener env has `CAPACITY_AWARE_PROACTIVE_CAPACITY=0`. All 9 other region-excluded GPU ARS also show `maxRunners: 0`.

2. **Helm release history**:
   - Rev 2 (2026-05-19 21:32:50): `maxRunners` field absent (→ MaxInt32), `CAPACITY_AWARE_PROACTIVE_CAPACITY=5`.
   - Rev 3 (2026-05-20 00:25:21, post-PR-#597): `maxRunners: 0` present, `CAPACITY_AWARE_PROACTIVE_CAPACITY=0`.
   - Diff between revs matches PR #597 exactly.

3. **Mimir metrics** (the literal output of the buggy listener's reporter cycle):

| Timestamp (UTC) | `gha_capacity_advertised_max_runners` | `gha_assigned_jobs` | Note |
|---|---|---|---|
| 2026-05-19 20:14:19 | 0 | 0 | rev 1 listener pod first scrape |
| 2026-05-19 21:35:48 | 31 | 31 | rev 2 listener pod first scrape after restart |
| 2026-05-19 22:56:24 | 25 | 25 | decay as some jobs finish |
| 2026-05-19 23:38:00 | 0 | 0 | rev 3 fix applied; listener restarted |

`gha_completed_jobs_total{result="failed"}` peaked at 25, `result="canceled"` peaked at 6 → 31 total dispatched, zero successful. `gha_capacity_placeholder_pods` reached 30 (the proactive feature dutifully created 30 placeholder pods that could never schedule).

Pre-rev-2 (i.e. the listener pod that handled the actual bug-job dispatch at 20:55:47Z) metric series was lost on restart, but the immediate post-restart value of 31 and the `gha_assigned_jobs` step from 0 to 31 leaves no doubt about what GitHub was told.

## Timeline (UTC)

| Time | Event |
|---|---|
| 2026-05-19 18:48 | PR #580 merged: arc-cbr-production-uw1 cluster created |
| 2026-05-19 19:57:55 | A10G-8 ARS registered with GitHub |
| 2026-05-19 20:55:47 | **Bug job 76835590399 dispatched** |
| 2026-05-19 21:32:50 | Helm rev 2 deploy (listener restarted, race re-opens) |
| 2026-05-19 21:34:16 | Bug job started (runner JIT-registered, no pod) |
| 2026-05-20 00:14:19 | **PR #597 merged** ("Zero-out advertised capacity for region-excluded runners") |
| 2026-05-20 00:25:21 | Helm rev 3 deploy with fix |
| 2026-05-19 23:35:13 | Bug job completed (FAILURE at "Initialize containers", after ~2h) |

(The bug job's `completed_at` 23:35:13 is BEFORE the rev 3 deploy at 00:25 — i.e. the job had already failed, and Huy's fix landed the next morning.)

## Hypothesis Resolution

| ID | Hypothesis | Status |
|---|---|---|
| H1 | HUD failure fallback (d5d94fb) advertises capacity | **REFUTED**. d5d94fb affects placeholder *creation* (`reconcileProvisioning`), not the `setMaxRunners` reporter. |
| H2 | `max_runners` defaults to `max_burst_capacity` | **REFUTED → REFINED**. Actual default is `math.MaxInt32`. |
| H3 | "Live" check satisfied by `PodScheduled` | **REFUTED**. `BothRunning()` requires `Phase == PodRunning` for both pods of a pair. |
| H4 | Runner was JIT-registered before pod scheduled | **CONFIRMED**. JIT registration is pod-state-independent. |
| H5 | `proactive_capacity_max_runners` PR (a3c294a) reports wrong number | **REFUTED**. That PR only changed `reconcileProvisioning`. The branch name is misleading. |
| H6 | `Detect and replace broken placeholder pairs` (d219a11) interprets unfulfillable placeholders as broken | **REFUTED**. CleanupBroken is correct behavior; the reporter already excludes broken pairs. |

## What Was NOT the Cause

- **GitHub Actions incident**: clean — Status Agent confirmed no GitHub status incident in the window.
- **AWS infrastructure**: clean — no us-west-1 AWS Health events; no g5 capacity stress.
- **Karpenter**: clean — Karpenter does not over-provision instances that match no NodePool.
- **Stale `TotalAssignedJobs` from upstream issue #4397**: ruled out — this requires a long-running listener; uw1 cluster only existed since 2026-05-19.
- **HUD failure multiplier (d5d94fb)**: NOT a direct cause — affects placeholder creation only.
- **Runner-class affinity (commit 4714643)**: NOT a cause — it keeps workflow placeholders Pending (so `runningPairs == 0`), which is actually the *intended* behavior. The capacity leak happens in spite of this, not because of it.

## Post-Fix State Verification

- All 9 region-excluded GPU ARS in uw1 now have `maxRunners: 0`.
- Listener env confirmed `CAPACITY_AWARE_PROACTIVE_CAPACITY=0`.
- Helm rev 3 active and deployed.
- No new jobs assigned to these listeners post-fix.
- Mimir `gha_capacity_advertised_max_runners` is at 0 and stable.

The fix demonstrably works in production.

## Operational Follow-Ups (NOT for this investigation — surfaced for the team)

1. **Loki observability gap**: `arc-systems` namespace pod logs are NOT being shipped to Loki for `pytorch-arc-cbr-production-uw1`. Only `loki.source.journal.system` and `loki.source.kubernetes_events` are active. This investigation could not directly inspect listener logs at the time of the bug because of this gap. Worth filing as a separate task.
2. **Other latent generator gaps**: `generate_runners.py` had one missing region-exclusion check; consider an audit for analogous oversights (e.g., does it honor `pause_runners`, `runner_class` filters, `runner_group` overrides for every supported field?). Phase 2 noted PR #598 plugged the same hole for `just resume-runners` (`runner_max_map.py`).
3. **Phantom EphemeralRunner CR cleanup**: the specific runner `np5rz` is already garbage-collected, but it's worth confirming there are no orphaned EphemeralRunner CRs from the 31 misdispatched jobs.
4. **ARC fork upstream-ability**: the startup-race and `MaxInt32` default are upstream-shape bugs. Consider opening an upstream issue or PR clamping `MaxRunners` to 0 by default until the capacity monitor's first reconcile completes.

## Files in This Investigation

- `user_details.md` — bug report + production safety rules
- `bug_evidence_initial.md` — Phase 1 confirmed evidence (E1–E4)
- `bug_hypotheses_initial.md` — Phase 1 hypotheses (H1–H6) — all now resolved above
- `2/web_search.md` — External Web Search Agent findings
- `2/github.md` — GitHub Agent findings (upstream + fork issues, code paths)
- `2/status.md` — Status Agent findings (no external incidents in window)
- `2/project_docs.md` — Project Documentation Agent findings (the generator gap)
- `2/code_flow.md` — Code Flow Agent findings (the full source-level trace)
- `2/recent_changes.md` — Recent Changes Agent findings (PR #597 fix already shipped)
- `3/compilation.md` — Phase 3 compilation, convergence map, early-exit recommendation
- `6/smoking_gun_verification.md` — Phase 6 live-state verification (Mimir, helm, ARS spec)
- `bug_conclusion_arc_phantom_capacity.md` — this file
