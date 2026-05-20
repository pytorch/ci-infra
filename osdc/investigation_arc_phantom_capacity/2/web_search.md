# External Web Search Agent — Phase 2 Findings

## Summary

The upstream `actions-runner-controller` listener reports capacity to GitHub by sending the configured `MaxRunners` value as the `X-ScaleSetMaxCapacity` HTTP header on every long-poll `GetMessage` call. GitHub's Actions broker uses this header value (NOT live runner/pod state) to decide how many jobs to dispatch to a scale set. The fork at `jeanschmidt/actions-runner-controller` adds a "capacity-aware placeholder pod" feature (`CAPACITY_AWARE_ENABLED`) that overrides this via `listener.SetMaxRunners(capacity)` where `capacity = runningRunners + runningPairs` (only counting Running placeholders where BothRunning is true). However, the listener is constructed with `MaxRunners: config.MaxRunners` (the static configured ceiling) and the capacity monitor goroutine is started in parallel with the listener goroutine via `errgroup.WithContext` with **no explicit synchronization** — creating a startup race window where the listener can poll GitHub with the full `config.MaxRunners` BEFORE the capacity monitor reduces it to 0. This race window matches the observed symptom exactly: GitHub dispatched a job to a scale set whose listener was advertising capacity for a runner type that could never schedule in the cluster.

## Findings

### Finding 1: Capacity is reported via the `X-ScaleSetMaxCapacity` HTTP header, sourced from configured `MaxRunners` (not live state)

- **Confidence**: high
- **Severity**: critical
- **Detail**: The upstream protocol documented by GitHub at `actions/scaleset` (the Go client for the Actions service that ARC uses) explicitly states: "When polling for messages, include your scale set's maximum capacity via the maxCapacity parameter (sent as the X-ScaleSetMaxCapacity header). This allows the backend to assign jobs accurately and avoid creating backlogs your scale set cannot fulfill." The upstream commit `e62d158` ("Propagate capacity information to the actions service") that landed via PR #3431 in v0.9.1 wires the listener config field as: `maxCapacity: config.MaxRunners // The maximum number of runners that can be created.` and passes it on every long-poll: `msg, err := l.client.GetMessage(ctx, l.session.MessageQueueUrl, l.session.MessageQueueAccessToken, l.lastMessageID, l.maxCapacity)`. **This is sourced directly from configuration, not from any dynamic count of live runners or pods.** GitHub's broker treats this as the truth and dispatches jobs accordingly.
  - https://github.com/actions/scaleset
  - https://github.com/actions/actions-runner-controller/pull/3431
  - https://github.com/actions/actions-runner-controller/commit/e62d158

### Finding 2: Startup race in jeanschmidt fork: listener polls GitHub with full `config.MaxRunners` before capacity monitor can override

- **Confidence**: high
- **Severity**: critical (this is the most plausible root cause)
- **Detail**: The fork's `cmd/ghalistener/main.go` constructs the listener with `MaxRunners: config.MaxRunners` and immediately launches it as a goroutine. The capacity monitor (which calls `listener.SetMaxRunners`) is launched as a sibling goroutine via `errgroup.WithContext`. There is **no synchronization** ensuring `SetMaxRunners(0)` is invoked before the listener's first `GetMessage` poll. From the fork's `capacity/monitor.go` `Run()` function, the initial `reconcileReporting(ctx)` (which would call `setMaxRunners(0)` since no placeholders are BothRunning yet at cold start) runs sequentially inside `Run()` — but this happens in a separate goroutine than the listener. If the listener's goroutine is scheduled first and reaches its first `GetMessage` (which sends the full `config.MaxRunners` in `X-ScaleSetMaxCapacity`) before the capacity monitor's goroutine reaches `setMaxRunners(0)`, GitHub will perceive full capacity for whatever brief window exists. Worse: `reconcileReporting` early-exits without calling `setMaxRunners` at all if listing placeholder pairs or counting runner pods fails after `reporterMaxRetries` (2 retries) — in that case the listener keeps its initial `config.MaxRunners` value indefinitely.
  - https://github.com/jeanschmidt/actions-runner-controller/blob/master/cmd/ghalistener/main.go (verified via WebFetch)
  - https://github.com/jeanschmidt/actions-runner-controller/blob/master/cmd/ghalistener/capacity/monitor.go (verified via WebFetch)

### Finding 3: Fork explicitly fixed an earlier "capacity doubling" bug — confirms the team already recognized this class of issue

- **Confidence**: high
- **Severity**: critical (historical evidence of the same bug class)
- **Detail**: Fork commit `f6c56d3` (April 30, 2026) is titled "Add MaxBurstCapacity and MaxRunners headroom" — the description shows the reporter loop subtracts actual `Running+Pending` runner pods from MaxRunners before clamping to prevent capacity doubling. This proves the maintainer was aware that miscalculating advertised capacity directly causes GitHub to dispatch jobs that can't be served. The split into provisioner+reporter loops with separate tick intervals (commit `1944a96`) was specifically to make capacity reporting more responsive — but the startup race remains unaddressed.
  - https://github.com/jeanschmidt/actions-runner-controller/commits/master/cmd/ghalistener/capacity

### Finding 4: Stale `TotalAssignedJobs` from GitHub Actions service can cause permanent phantom demand (Issue #4397)

- **Confidence**: high
- **Severity**: critical (alternative root cause hypothesis)
- **Detail**: Upstream issue #4397 (`listener: stale TotalAssignedJobs from GitHub Actions service causes permanent over-provisioning after platform incidents`) documents a closely related failure mode: "After a GitHub Actions platform incident, the TotalAssignedJobs value in RunnerScaleSetStatistic gets stuck at an inflated count that never recovers. The listener passes this value directly to the scaling calculation, causing the controller to permanently over-provision runners for phantom demand. The only fix is deleting the AutoscalingRunnerSet CR — listener pod restarts don't clear the state because it comes from the GitHub Actions service, not the listener." If a job was assigned to uw1 during a transient capacity-reporting glitch (Finding 2), the stale-assignment behavior here could explain how the job persisted for 2h before failing. **Key takeaway: deleting the AutoscalingRunnerSet CR forces a complete deregistration and a fresh session — a listener pod restart alone does not clear inflated TotalAssignedJobs.** This may be relevant if the user wants to remediate the live cluster.
  - https://github.com/actions/actions-runner-controller/issues/4397

### Finding 5: Same-named scale sets across regions are routed by GitHub to runner groups arbitrarily — no deterministic region pinning

- **Confidence**: high
- **Severity**: moderate (informs how a job CAN end up at a specific listener)
- **Detail**: GitHub's official docs and issue #4385 confirm: "If both runner scale sets are online, jobs assigned to them will be distributed arbitrarily (assignment race). You cannot configure the job assignment algorithm." The same-named-scale-sets-in-different-runner-groups pattern is the OFFICIALLY SUPPORTED HA setup. Workflows target the scale set by NAME, and the broker selects ANY listener whose runner-group policy allows the workflow's repo. **Implication for this bug**: even if the scale set name `mt-l-x86aavx2-189-704-a10g-8` exists in multiple regions, GitHub's broker will pick whichever scale set's listener is advertising capacity AND whose runner group includes the source repo. There is no region-awareness — if uw1's listener advertised capacity (Finding 2), and the repo was eligible for the `arc-cbr-prod-uw1` runner group, GitHub had no reason to send the job elsewhere.
  - https://github.com/actions/actions-runner-controller/issues/4385
  - https://docs.github.com/en/actions/how-tos/manage-runners/use-actions-runner-controller/deploy-runner-scale-sets

### Finding 6: GitHub does NOT verify runner pod health before assigning jobs

- **Confidence**: high
- **Severity**: critical (rules out a "GitHub should have noticed" defense)
- **Detail**: From upstream README and the scaleset client docs: GitHub's only signal that a scale set can serve a job is the `X-ScaleSetMaxCapacity` header on long-poll requests. There is NO verification that runner pods are alive, schedulable, or that nodes exist. From the docs: "After 24 hours the Actions Service unassigns the job if no runner accepts it" and "this can happen up to 3 times with incremental delays" for job reassignment. The 2-hour duration on the failing job is consistent with GitHub waiting for a runner pod that would never come up.
  - https://github.com/actions/actions-runner-controller/blob/master/docs/gha-runner-scale-set-controller/README.md
  - https://github.com/actions/scaleset

### Finding 7: JIT registration is requested by the controller (not the runner) BEFORE the pod schedules

- **Confidence**: high
- **Severity**: moderate
- **Detail**: This is significant for the symptom: the EphemeralRunner controller calls `GenerateJitRunnerConfig` to obtain a JIT registration token AND the runner is "registered" with GitHub at that point — the pod itself merely uses the token to authenticate later. So in the failure scenario described, the EphemeralRunner CR could exist, a JIT token could be issued (making the runner appear "registered" to GitHub), but the pod could be Pending forever because no Karpenter NodePool would provision g5 in us-west-1. The job would sit assigned to that "registered" runner until the 24h timeout (or until init-containers fail at ~2h as observed).
  - https://github.com/actions/actions-runner-controller/blob/master/docs/gha-runner-scale-set-controller/README.md

### Finding 8: Fork's reporter loop early-exits on Kubernetes API errors, leaving previous (potentially configured) maxCapacity intact

- **Confidence**: high
- **Severity**: critical
- **Detail**: From the verified `reconcileReporting` source code in the fork: "On failure, keep previous capacity unchanged." If the very first invocation at startup fails (e.g. transient k8s API unavailability when listing pods or placeholders), `setMaxRunners` is never called, and the listener continues advertising `config.MaxRunners` to GitHub indefinitely. The fork tracks this with skip counters (`skipReasonReporterListPairs`, `skipReasonReporterCountRunners`) — these would be visible in metrics if alerts were configured.
  - https://github.com/jeanschmidt/actions-runner-controller/blob/master/cmd/ghalistener/capacity/monitor.go

### Finding 9: 0.10.0 added a "self-correction on empty batch" feature (PR #3426) — but doesn't address proactive capacity behavior

- **Confidence**: medium
- **Severity**: minor
- **Detail**: Upstream PR #3426 added: "Include self correction on empty batch and avoid removing pending runners when cluster is busy" — this is upstream's "we know capacity reporting can be wrong" mitigation. But it acts on the controller side (avoiding premature pending runner cleanup), NOT on the listener side (preventing wrong values from being advertised). The fork has not pulled equivalent listener-side safeguards.
  - https://github.com/actions/actions-runner-controller/discussions/3850 (gha-runner-scale-set-0.10.0)

### Finding 10: Karpenter does NOT silently provision wrong instance types — rules out infra-level confusion

- **Confidence**: high
- **Severity**: minor (rules out a hypothesis)
- **Detail**: Multiple Karpenter issues (kubernetes-sigs/karpenter#2275, aws/karpenter-provider-aws#6168, #6483, #8885) confirm Karpenter's well-known fallback failure modes — but none describe Karpenter ever provisioning an instance type that doesn't match any NodePool. The known bugs are about Karpenter REFUSING to fall back, not erroneously over-provisioning. So Karpenter is innocent here: if no NodePool allows g5 in us-west-1, Karpenter would correctly refuse to provision, leaving the runner pod Pending forever (consistent with "failed at Initialize containers" after a long wait).
  - https://github.com/kubernetes-sigs/karpenter/issues/2275
  - https://github.com/aws/karpenter-provider-aws/issues/6168

### Finding 11: No CVE or security advisory touches the listener capacity protocol

- **Confidence**: high
- **Severity**: minor (rules out a hypothesis)
- **Detail**: Searched the actions/actions-runner-controller security overview page and broader CVE/GHSA databases. No advisories found that touch the listener-side capacity reporting protocol. This is an architectural/correctness issue, not a security issue.
  - https://github.com/actions/actions-runner-controller/security

### Finding 12: The fork's "placeholder" terminology is unique — upstream uses `minRunners` (which holds a license slot per warm runner)

- **Confidence**: high
- **Severity**: minor (terminology clarification)
- **Detail**: Upstream issue #2707 (`gha-runner-scale-set - pre-emptively scale runners based on new "min idle runners" setting`) is the open request for a NATIVE proactive capacity feature. It remains unmerged. The community workaround is either (a) `minRunners > 0` to keep warm runners (consumes runner slots), or (b) external low-priority placeholder Pods (the GKE pattern) for node pre-warming WITHOUT GitHub registration. **The jeanschmidt fork's approach is NOVEL**: it creates runner+workflow placeholder pod PAIRS and ties capacity advertisement to their Running state — a design upstream has not adopted. This means there is essentially zero community discussion of the specific bug class this fork could introduce.
  - https://github.com/actions/actions-runner-controller/issues/2707

## Open Questions

1. **What was `config.MaxRunners` on the uw1 listener for `mt-l-x86aavx2-189-704-a10g-8` at the time of the incident?** This determines whether the startup race could have advertised >0 capacity. Worth checking the AutoscalingRunnerSet CR on the cluster.
2. **Did the listener restart shortly before the job assignment?** Pod restart timestamps should be cross-referenced with the GitHub job assignment timestamp. A restart immediately preceding the assignment would strongly support the startup-race hypothesis.
3. **Were any `skipReasonReporterListPairs` / `skipReasonReporterCountRunners` metrics non-zero?** If the very first `reconcileReporting` skipped, this would be the smoking gun.
4. **Did GitHub mark a runner as "registered" before the job was assigned?** If the EphemeralRunner controller already issued a JIT token (Finding 7), the runner could be visible to GitHub even though no pod existed.
5. **Could a single past-tense `setMaxRunners(>0)` call (e.g. during a brief window where a placeholder pair was Running) have left a stale value that subsequent skipped reconcile cycles never corrected?** Worth checking the Run() / shutdown ordering — there's an acknowledged "flash of reportedCapacity=0" risk at shutdown that the code handles, but the same author noted no startup mitigation.
6. **Is the `arc-cbr-prod-uw1` runner group restricted to a specific set of repositories?** If yes, narrowing the dispatch to "any listener with capacity" + "any listener in this runner group" still doesn't explain how the broker could pick a listener that never had real capacity unless the listener actively advertised it.

## Recommended Next Steps

1. **Verify the live listener's advertised capacity vs. live runner counts** via the fork's Prometheus metrics: `gha_advertised_max_runners` should equal `runningPairs + runningRunners` at all times. Drift = bug.
2. **Inspect the failed listener pod's logs at startup** for the very first `GetMessage` call — it should log the `maxCapacity` it sent. Compare against placeholder pod startup times.
3. **Add a SetMaxRunners(0) BEFORE listener.Listen() starts** in the fork's `main.go`. The listener should not be allowed to poll GitHub until the capacity monitor has explicitly set 0 (or the configured value if capacity-aware is disabled). This is a 5-line fix that closes the race definitively.
4. **Add a "first-poll guard"** in the listener: do not send any non-zero `X-ScaleSetMaxCapacity` until `setMaxRunners` has been called at least once externally. Treat the constructor's `config.MaxRunners` as a HARD CEILING only, with the runtime value defaulting to 0.
5. **Check upstream issue #4397's workaround**: if the live uw1 cluster has phantom assigned jobs accumulating, delete the AutoscalingRunnerSet CR to force a complete deregistration. Listener pod restart is insufficient.
6. **Cross-reference with Karpenter NodePool config** to confirm g5 is truly excluded from us-west-1 — if it is, the failure mode is exactly what's expected when GitHub dispatches a job to a scale set that can never serve it.
7. **Audit other capacity-aware fork features** for similar startup race patterns (the same `errgroup.WithContext` parallel goroutine pattern would affect any subsystem that needs to gate the listener).

## Sources

- [actions/scaleset Go client](https://github.com/actions/scaleset)
- [actions-runner-controller upstream](https://github.com/actions/actions-runner-controller)
- [PR #3431 - Propagate max capacity to actions back-end](https://github.com/actions/actions-runner-controller/pull/3431)
- [Commit e62d158 - capacity propagation source code](https://github.com/actions/actions-runner-controller/commit/e62d158)
- [Issue #4397 - stale TotalAssignedJobs](https://github.com/actions/actions-runner-controller/issues/4397)
- [Issue #2707 - minIdleRunners request](https://github.com/actions/actions-runner-controller/issues/2707)
- [Issue #4385 - region pinning](https://github.com/actions/actions-runner-controller/issues/4385)
- [Issue #4004 - uneven distribution](https://github.com/actions/actions-runner-controller/issues/4004)
- [Discussion #3850 - gha-runner-scale-set 0.10.0](https://github.com/actions/actions-runner-controller/discussions/3850)
- [PR #3426 - self correction on empty batch](https://github.com/actions/actions-runner-controller/pull/3426)
- [ARC 0.14.0 release notes](https://github.blog/changelog/2026-03-19-actions-runner-controller-release-0-14-0/)
- [jeanschmidt fork - capacity package commits](https://github.com/jeanschmidt/actions-runner-controller/commits/master/cmd/ghalistener/capacity)
- [jeanschmidt fork - monitor.go](https://github.com/jeanschmidt/actions-runner-controller/blob/master/cmd/ghalistener/capacity/monitor.go)
- [jeanschmidt fork - main.go](https://github.com/jeanschmidt/actions-runner-controller/blob/master/cmd/ghalistener/main.go)
- [Karpenter ICE retry / fallback issues](https://github.com/kubernetes-sigs/karpenter/issues/2275)
- [ARC scale set protocol docs](https://github.com/actions/actions-runner-controller/blob/master/docs/gha-runner-scale-set-controller/README.md)
- [ARC security overview](https://github.com/actions/actions-runner-controller/security)
- [Ken Muse - What's new in GitHub ARC](https://www.kenmuse.com/blog/whats-new-in-github-arc/)
