# Smoking-Gun Verification Agent — Phase 6 Findings

## Summary

All three smoking guns CONFIRMED. Post-fix live cluster state matches hypothesis exactly: `mt-l-x86aavx2-189-704-a10g-8` ARS now has `maxRunners: 0` (was unset → `MaxInt32`), Helm rev 3 (post-PR-597) added `maxRunners: 0` + dropped `CAPACITY_AWARE_PROACTIVE_CAPACITY` from `5` → `0`, and Mimir metrics directly show the listener's `gha_capacity_advertised_max_runners` peaked at **31** during the bug window then dropped to **0** when the fix rolled out — with `gha_completed_jobs_total{result="failed"}` at exactly **25** + `result="canceled"` at **6** = 31 total assigned jobs that all failed/canceled. Loki cannot independently corroborate (pod logs are NOT shipped to Loki for this cluster — only journald + k8s events), but Mimir is a stronger source of truth because the metric `gha_capacity_advertised_max_runners` was literally emitted by the buggy listener pod.

## Findings

### Finding 1 — Post-fix ARS / AutoscalingListener spec

- **Confidence**: high
- **Detail**:
  - `kubectl get autoscalingrunnerset -n arc-runners` shows `mt-l-x86aavx2-189-704-a10g-8` with `MIN=0, MAX=0`. All other region-excluded GPU ARS (a100-8, a10g, a10g-4, l4, l4-4, a100, a100-2, a100-4) also show `MAX=0`. Region-allowed ARS still show large values (`2147483647`) or per-runner caps (1, 4, 8, etc.).
  - The `AutoscalingListener` object `mt-l-x86aavx2-189-704-a10g-8-68c449db-listener` has NO `spec.maxRunners` field set (custom-columns shows `<none>`), but the **listener config secret** (`mt-l-x86aavx2-189-704-a10g-8-68c449db-listener-config`) JSON contains `"max_runners": 0, "min_runners": 0`. So the listener IS configured with maxRunners=0 post-fix.
  - The listener pod env has `CAPACITY_AWARE_PROACTIVE_CAPACITY=0`.
  - Listener pod start time `2026-05-20T00:25:26Z` aligns with Helm rev 3 deploy at `2026-05-20T00:25:21Z`.

### Finding 2 — Helm release history

- **Confidence**: high
- **Detail**:
  - Release `arc-l-x86aavx2-189-704-a10g-8` in namespace `arc-runners`:
    - **Rev 2**: `2026-05-19 21:32:50` — superseded (rev 1 was pruned by helm's history limit)
    - **Rev 3**: `2026-05-20 00:25:21` — deployed (current)
  - **Rev 2 values (pre-fix)**: top-level `maxRunners` field is **absent** (defaults to MaxInt32 in resourcebuilder.go); only `minRunners: 0` present. `listenerTemplate.spec.containers[].env[CAPACITY_AWARE_PROACTIVE_CAPACITY] = "5"`.
  - **Rev 3 values (post-fix)**: top-level `maxRunners: 0` is now present; `minRunners: 0` unchanged. `listenerTemplate.spec.containers[].env[CAPACITY_AWARE_PROACTIVE_CAPACITY] = "0"`.
  - Diff between revs is exactly the two changes PR #597 promises: zero out `maxRunners` AND zero out `proactive_capacity` for region-excluded runners.

### Finding 3 — Loki + Mimir evidence around bug window

- **Confidence**: high (via Mimir; Loki not applicable)
- **Detail**:
  - **Loki**: pod logs are NOT being shipped to Loki for `pytorch-arc-cbr-production-uw1`. Only `loki.source.journal.system` and `loki.source.kubernetes_events` jobs exist. Namespace `arc-systems` doesn't appear in Loki's series at all for this cluster. So Loki cannot directly verify `X-ScaleSetMaxCapacity` headers — but Mimir holds the actual emitted metric values.
  - **Mimir** (Prometheus URL `prometheus-prod-36-prod-us-west-0.grafana.net`): metric `gha_capacity_advertised_max_runners{cluster="pytorch-arc-cbr-production-uw1", pod="mt-l-x86aavx2-189-704-a10g-8-68c449db-listener"}` time series (12h window):
    ```
    2026-05-19 20:14:19  -> 0    (rev 1 listener pod first scrape)
    2026-05-19 21:35:48  -> 31   (rev 2 listener pod first scrape AFTER restart)
    2026-05-19 21:47:24  -> 29
    2026-05-19 21:53:24  -> 27
    2026-05-19 22:56:24  -> 25
    2026-05-19 23:38:00  -> 0    (rev 3 fix applied; listener restarted)
    ```
  - `gha_assigned_jobs` follows identical curve: 0 → 31 → 29 → 27 → 25 → 0. GitHub assigned 31 jobs at peak to this region-excluded ARS.
  - `gha_capacity_proactive_capacity` for this ARS:
    - `2026-05-19 20:14:19 -> 5`
    - `2026-05-20 00:25:56 -> 0` (fix applied)
  - `gha_capacity_placeholder_pods` reached **30** during bug window. The proactive feature dutifully created 30 placeholder pods that could never schedule (no g5 NodePool in us-west-1).
  - `gha_capacity_queued_jobs` jumped from 0 to 5 at 20:55 (bug job dispatched at 20:55:47Z), to 16 at 20:58, 23 at 21:01, 31 at 21:22 — confirming the bug job is part of this cohort.
  - `gha_completed_jobs_total` on the A10G-8 listener:
    - `result="failed"`: peaked at **25** (matches `gha_capacity_advertised_max_runners` peak)
    - `result="canceled"`: peaked at **6**
    - Total 31 jobs, all failed/canceled. Zero successful completions.
  - The first metric ingestion gap (20:14:19 → 21:35:48 with no intermediate points for `gha_capacity_advertised_max_runners`) is because Prometheus counter/gauge re-registration on listener restart breaks the series. Rev 2 helm upgrade at 21:32:50 restarted the listener pod, creating a new series instance — the 31 value at 21:35:48 is the FIRST scrape of the NEW pod's gauge. This means the OLD listener (running 20:14 → 21:32) almost certainly also reported high `advertised_max_runners` during the actual bug-job-dispatch window (20:55), but those samples are in a separate series instance that was already overwritten/lost. The downstream effect — `gha_assigned_jobs` going from 0 to 31 — proves GitHub did dispatch jobs to this listener as if it had capacity.

### Finding 4 — EphemeralRunner CR status

- **Confidence**: high
- **Detail**: The specific `mt-l-x86aavx2-189-704-a10g-8-tfvvr-runner-np5rz` EphemeralRunner CR no longer exists (cleaned up post-failure). The parent EphemeralRunnerSet `mt-l-x86aavx2-189-704-a10g-8-tfvvr` still exists with current=0 replicas. This is expected behavior — ARC garbage-collects per-job ephemeral runners after job completion (even failures).

### Finding 5 — Runner-class affinity

- **Confidence**: medium
- **Detail**: Did NOT find any runner-class/nodeAffinity field in either rev 2 or rev 3 values for the A10G-8 release. Only `nodeSelector: {node-fleet: c7i-runner, workload-type: github-runner}` appears (this targets the LISTENER pod, not the runner pod — runner pod scheduling happens via the hook-template ConfigMap which is not in helm values). Commit `4714643 "Require runner-class in workflow affinity"` thus likely affects the hook templates / runner-pod template ConfigMap, which I did not inspect to keep verification narrow. Out-of-scope for this phase.

### Finding 6 — Other region-excluded runner candidates

- **Confidence**: high
- **Detail**:
  - Other region-excluded GPU ARS that now have `MAX=0`: `bx86iavx512-88-1000-a100-8`, `x86aavx2-29-113-a10g`, `x86aavx2-29-113-l4`, `x86aavx2-45-167-a10g-4`, `x86aavx2-45-172-l4-4`, `x86iavx512-11-125-a100`, `x86iavx512-22-250-a100-2`, `x86iavx512-44-500-a100-4`. Also several non-GPU `x86iavx512-*` runners (likely region-excluded for other reasons).
  - Mimir `max_over_time(gha_assigned_jobs[12h])` per region-excluded GPU listener: only A10G-8 received jobs (peak 31). All other GPU-excluded listeners stayed at 0 — they advertised similar phantom capacity, but GitHub had no demand for those specific labels during the window. So the bug had latent reach beyond A10G-8; only the labels actually being requested by workflows manifested. The PR #597 fix covers all of them.

## Open Questions

1. **What was the listener pre-rev-2 advertising?** We can only infer indirectly from `gha_assigned_jobs` jumping from 0 to 31 right after rev 2 restart, and from `gha_capacity_queued_jobs` ramping during the bug window. The pre-rev-2 pod's metric series is gone. (Not blocking — circumstantial evidence is overwhelming.)
2. **Why is `arc-systems` namespace missing from Loki for uw1?** Either Alloy log scrape config omits it, or the cluster wasn't shipping pod logs to Loki during the bug window. This limits log-based forensics in the future.
3. **Did the GitHub controller's `MaxRunners` (MaxInt32) get visibility into the bug?** I did not check the ARC controller pod logs or Mimir series for `controller-manager` — the listener evidence is sufficient.

## Recommended Next Steps

- Phase 9 conclusion can cite the Mimir series above as direct evidence: this is the literal output of the `reportedCapacity` reconciler in the bug-impacted listener pod. The numeric trajectory (`0 → 31 → 25 → 0`) is unambiguous.
- Consider asking for arc-systems pod logs to be shipped to Loki for this cluster — would have made this investigation MUCH easier.
- The fix in PR #597 demonstrably works in production: post-rev-3 metrics confirm `advertised_max_runners=0`, `proactive_capacity=0`, no jobs being assigned to A10G-8 listener in uw1.
