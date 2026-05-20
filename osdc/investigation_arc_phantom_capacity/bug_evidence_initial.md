# Confirmed Evidence — Initial (Phase 1)

Append-only. Each entry must cite the source (agent name + phase) and how it was confirmed.

## E1 — Job ran on uw1 cluster (Phase 1, orchestrator)

- GitHub job 76835590399 reports `runner_group_name: arc-cbr-prod-uw1`, `runner_group_id: 85`.
- `clusters.yaml` shows `arc-cbr-production-uw1.arc-runners.runner_group: arc-cbr-prod-uw1`.
- Confirmed via `gh api repos/pytorch/pytorch/actions/jobs/76835590399`.

## E2 — Runner type is g5.48xlarge, NodePool excludes us-west-1 (Phase 1, orchestrator)

- `modules/arc-runners/defs/l-x86aavx2-189-704-a10g-8.yaml`: `instance_type: g5.48xlarge`, `proactive_capacity: 5`, `max_burst_capacity: 30`.
- `modules/nodepools/defs/g5.yaml`: `exclude_regions: [us-west-1]`.
- Confirmed via direct file read.

## E3 — Job failed at "Initialize containers" after ~2h (Phase 1, orchestrator)

- Created 20:55:47Z, Started 21:34:16Z, Completed 23:35:13Z.
- Conclusion: `failure`. Failing step: `Initialize containers`.
- Confirmed via GitHub API.

## E4 — `arc-runners` module IS deployed in uw1; `nodepools` IS deployed in uw1 (Phase 1, orchestrator)

- `clusters.yaml` `arc-cbr-production-uw1.modules` includes both.
- The runner generator has no per-cluster filter for unfulfillable runner defs (verified by file listing; deeper code review owed to phase 2).
