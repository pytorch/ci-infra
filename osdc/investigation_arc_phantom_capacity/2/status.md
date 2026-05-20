# Status Agent — Phase 2 Findings

## Summary

**No external service incidents correlate with the bug window** (2026-05-19 20:00 UTC to 2026-05-20 02:00 UTC). The GitHub Status page, three independent third-party monitors (StatusGator, IsDown, UptimeRobot, ServiceAlert.ai), and AWS Health for us-west-1 (EC2/EKS/EventBridge/IAM) are all clean for that window. The closest prior GitHub Actions incident was May 15, 2026 (~4 days before the job creation at 20:55:47 UTC). Environmental factors can be largely **ruled out** as the proximate cause, though there is one indirect risk vector worth noting (see Finding 4 — ARC stale-state issue #4397 chained from a prior platform incident).

## Findings

### Finding 1: GitHub Status — no incidents in bug window

- **Confidence**: high
- **Severity**: minor (ruling out, not a hit)
- **Detail**:
  - Verified via GitHub's own status page (https://www.githubstatus.com/), history feed (https://www.githubstatus.com/history.atom), and JSON API (https://www.githubstatus.com/api/v2/incidents.json) — no incidents reported between 2026-05-19 00:00 UTC and 2026-05-20 23:59 UTC.
  - Feed last update timestamp captured: `2026-05-19T22:18:40Z` — which is INSIDE the bug window (20:00 UTC – 02:00 UTC). At that time GitHub themselves reported no active incident.
  - All 11 monitored components (Git Operations, API Requests, Webhooks, Issues, Pull Requests, Actions, Packages, Pages, Codespaces, Copilot, Copilot AI Model Providers) were Operational as of the snapshot.
  - 90-day uptime for Actions: 99.7% (i.e. not historically anomalous).
  - Cross-validated via three independent monitors:
    - **StatusGator**: GitHub operational at 2026-05-19 08:06 UTC; 14 user-submitted reports in past 24h (low signal, no acknowledged incident).
    - **UptimeRobot**: clean check at 2026-05-19 06:40 UTC (North America probe).
    - **IsDown**: 3 user reports in past 24h as of 2026-05-19 06:35 EDT.
    - **ServiceAlert.ai**: last GitHub status change recorded 2026-05-15 08:50 UTC; checked 2026-05-19 at 21:15 UTC clean.

### Finding 2: Closest prior GitHub Actions incident — May 15, 2026 (4 days before bug)

- **Confidence**: high
- **Severity**: moderate (timing-adjacent, see Finding 4 for the chained-state risk)
- **Detail**:
  - **2026-05-15 07:43–08:48 UTC** — "Actions degraded availability." Planned infrastructure failover; automated service discovery update did not propagate correctly; traffic mis-routed; timeouts in a core workflow orchestration dependency. Peak impact: **42% of Actions runs failed.** Pages and Copilot cloud also affected. Mitigation: manual service-discovery correction at 08:12 UTC.
  - Other May 2026 Actions-related incidents (all > 4 days before the bug):
    - 2026-05-06 06:45–09:15 UTC — Standard Ubuntu hosted runners; 17.1% job failure rate; bad allocation config data from prior remediation.
    - 2026-05-05 13:22–17:05 UTC — Hosted runners East US degraded; 13.5% standard / ~16% Larger Runners failures.
  - **None of these touched the runner scale set listener API, runner group routing, or self-hosted runner registration.** All were hosted-runner allocation or service-discovery problems on GitHub-managed infrastructure.

### Finding 3: AWS Health (us-west-1) — clean for the bug window

- **Confidence**: high
- **Severity**: minor (ruling out)
- **Detail**:
  - `aws health describe-events` for `regions=us-west-1` with window `2026-05-19T18:00Z` → `2026-05-20T03:00Z` returned `events: []`.
  - Same query against `regions=global` for the same window returned `events: []`.
  - Broader us-west-1 EC2/EKS event scan returned only routine items: scheduled EC2 retirements (2026-05-13, closed), ECS task patching retirements (informational), ODCR underutilization notifications (March–April), and an EKS planned lifecycle event (upcoming 2026-07-29). **Nothing within the bug window.**
  - AWS credentials verified working (`sts get-caller-identity` succeeded as `SSOAdmin/jschmidt@meta.com` account 308535385114).
  - The major 2026 AWS outage was **us-east-1, not us-west-1** — thermal event on 2026-05-07/08 in az `use1-az4`. Confirmed irrelevant to this bug (different region).

### Finding 4: Indirect risk — ARC stale-state issue chained from prior platform incidents (issue #4397)

- **Confidence**: medium
- **Severity**: moderate (relevant pattern, but does NOT match this specific bug shape)
- **Detail**:
  - actions/actions-runner-controller#4397 ("listener: stale TotalAssignedJobs from GitHub Actions service causes permanent over-provisioning after platform incidents") documents that after a GitHub Actions platform incident, `RunnerScaleSetStatistic.TotalAssignedJobs` can get stuck at an inflated value. The listener consumes this directly with no reconciliation against `TotalRunningJobs`, no TTL, and no staleness detection.
  - **Symptom shape**: phantom **demand-side** capacity — the controller provisions real runner pods to satisfy work that doesn't exist. Listener restarts do NOT clear it; only deleting the `AutoscalingRunnerSet` CR does.
  - **Why this is relevant**: a GitHub Actions platform incident occurred 2026-05-15 (Finding 2). If `arc-cbr-production-uw1`'s listener was running before that incident and was never restarted/CR-recreated, it could carry stale state into 2026-05-19. The bug shape under investigation (capacity advertised when none should exist) is **directionally consistent** with this class of bug.
  - **However**, issue #4397's symptom is over-provisioning **within a single scale set** (excess pods of the right type that have no work). It does NOT describe cross-runner-group dispatch, scale-set identity confusion, or jobs being routed to a scale set whose underlying instance type is excluded from the region. Those are different failure modes.
  - **Verdict**: this is a real pattern of "ARC misbehaves after GitHub incidents" worth flagging to the deeper-debug agents, but it is **not a sufficient explanation** for the observed bug on its own. Issue is "Closed, not planned, stale" — i.e. unfixed upstream.

### Finding 5: No evidence of g5 capacity shortage in us-west-1 during the window

- **Confidence**: medium (absence of evidence, not evidence of absence — AWS does not publish capacity data)
- **Severity**: minor
- **Detail**:
  - No web reports of `InsufficientInstanceCapacity` events for g5 in us-west-1 in May 2026.
  - AWS Health events for us-west-1 EC2 in the window show only routine retirements and ODCR underutilization summaries — nothing indicating regional capacity stress.
  - **Aside**: AWS public docs do not list G5 in the "US West (N. California)" accelerated-computing options. This aligns with OSDC's nodepool config excluding g5.48xlarge from us-west-1. Note: this is a region-level instance-type-availability constraint, NOT an incident — but it underscores the original architectural decision that the bug is violating.

### Finding 6: No evidence of GitHub runner registration API / listener API regression

- **Confidence**: medium
- **Severity**: minor
- **Detail**:
  - No search results surface a May 2026 GitHub change to the runner scale set listener authentication flow, runner registration API, or runner group enforcement logic.
  - Related listener pod recreation issue (actions/actions-runner-controller#4356, January 2026) describes "StatusCode 200" errors causing pod restarts in ARC 0.13.1 — unrelated to the dispatch routing observed here.

## Open Questions

1. **Was the `arc-cbr-production-uw1` listener pod running continuously through the 2026-05-15 GitHub Actions incident?** If yes, issue #4397 staleness is plausible as a contributor (not as a sole cause). The Phase 6 logs/live-state agent should check listener pod restart timestamps and `AutoscalingRunnerSet` CR creation/modify timestamps.
2. **What is the AWS Health event posture for the specific AWS account hosting `arc-cbr-production-uw1`?** My check ran against my default account (308535385114). If the OSDC prod cluster lives in a different account, AWS Health needs to be re-queried there.
3. **Did anyone change the runner group definition, scale set spec, or nodepool taints between 2026-05-15 and 2026-05-19?** Not an external-service question — flag to git/config-history agents.

## Recommended Next Steps

1. **Do not pursue the "GitHub platform was broken at the time" hypothesis as a primary theory.** External signals contradict it. Treat this as an OSDC/ARC-side bug.
2. **Have the Phase 6 logs agent capture**:
   - Listener pod uptime/restart history for `arc-cbr-production-uw1` (especially across 2026-05-15 07:43 UTC).
   - `AutoscalingRunnerSet` CR `creationTimestamp` and last-modify time for the scale set serving runner ID 42292023 (`mt-l-x86aavx2-189-704-a10g-8-tfvvr-runner-np5rz`).
   - Any `RunnerScaleSetStatistic` metrics if exported (`assigned_jobs`, `running_jobs`, `desired_runners`, `busy_runners`) at the time of the job pickup.
3. **Re-run AWS Health from the OSDC prod account** if cluster `arc-cbr-production-uw1` is not in account 308535385114. Use:
   ```
   aws health describe-events --filter "regions=us-west-1,startTimes=[{from=2026-05-19T18:00:00Z,to=2026-05-20T03:00:00Z}]"
   ```
4. **Add issue #4397 to the deeper-debug reading list** for the Likely Culprits agent — even though it doesn't fully match the bug shape, the pattern of "stale GitHub-side state survives listener restarts" is the kind of thing that could compose with another OSDC-side bug to produce the observed behavior.

## Sources

- [GitHub Status](https://www.githubstatus.com/)
- [GitHub Status — History feed](https://www.githubstatus.com/history.atom)
- [GitHub Status — Incidents JSON API](https://www.githubstatus.com/api/v2/incidents.json)
- [GitHub Outages 2025–2026: Reliability Analysis (incidenthub)](https://blog.incidenthub.cloud/github-reliability-outage-history-2025-2026)
- [StatusGator GitHub status](https://statusgator.com/services/github)
- [IsDown GitHub status](https://isdown.app/status/github)
- [ServiceAlert.ai GitHub status](https://servicealert.ai/status/github)
- [actions/actions-runner-controller#4397 — stale TotalAssignedJobs](https://github.com/actions/actions-runner-controller/issues/4397)
- [actions/actions-runner-controller#4356 — listener pods recreating](https://github.com/actions/actions-runner-controller/issues/4356)
- [AWS Health Dashboard](https://health.aws.amazon.com/health/status)
- [The Register — AWS us-east-1 thermal event May 2026](https://www.theregister.com/off-prem/2026/05/08/aws-warns-of-ec2-impairment-as-power-loss-hits-notorious-us-east-1-region/5235509)
- [AWS EC2 instance types by Region — G5 availability docs](https://docs.aws.amazon.com/ec2/latest/instancetypes/ec2-instance-regions.html)
