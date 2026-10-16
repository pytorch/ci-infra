# Pre-merge CI

## What this is

The pre-merge CI runs the OSDC validation battery against the `meta-staging-aws-uw1` cluster when PRs enter the merge queue. It is the gate that determines whether a PR is allowed to merge into `main`.

## Workflow shape

The workflow is defined in `.github/workflows/osdc-pre-merge.yml` and triggers on `merge_group` and `workflow_dispatch`. It is composed of four jobs:

1. `changes` — uses `dorny/paths-filter` to detect whether the PR touches osdc-related files. Outputs `osdc: true|false`.
2. `deploy-fast` — calls reusable `_osdc-deploy.yml` (lint+test → deploy `meta-staging-aws-uw1` → smoke + integration tests). Runs only when osdc files changed.
3. `slow-tests` — calls reusable `_osdc-slow-tests.yml` (load-test → compactor + janitor → drain/undrain). Runs after `deploy-fast` succeeds. Informational only. Drain/undrain waits for both compactor and janitor, because it changes runner limits cluster-wide.
4. `pre-merge-ok` — marker job that always runs and reports SUCCESS when the gate is satisfied.

Dependency chain:

```
┌─────────┐     ┌─────────────┐     ┌────────────┐
│ changes │───▶ │ deploy-fast │───▶ │ slow-tests │
└────┬────┘     └──────┬──────┘     └────────────┘
     │                 │
     │                 ▼
     │          ┌──────────────┐
     └────────▶ │ pre-merge-ok │
                └──────────────┘
```

`pre-merge-ok` depends on `changes` and `deploy-fast`, but NOT on `slow-tests`.

## Fast vs slow jobs

| Aspect            | Fast (`deploy-fast`)                       | Slow (`slow-tests`)                  |
|-------------------|--------------------------------------------|--------------------------------------|
| What it runs      | lint + test, deploy meta-staging-aws-uw1, smoke, integration | load-test, compactor e2e, janitor e2e, drain/undrain |
| Gates merge?      | Yes (via `pre-merge-ok`)                   | No (informational only)              |
| Typical duration  | ~30-60 min                                 | ~2.5-4 h                             |
| Failure effect    | Blocks merge                               | Surfaces red check, merge still allowed |

## The marker job

`pre-merge-ok` is the SOLE required check on the merge queue. It always runs (no path filter) and reports SUCCESS when either:

- the path filter excluded the PR (no osdc changes), or
- `deploy-fast` succeeded.

It is independent of `slow-tests` — slow failures do not flip the marker red.

Without this marker, the merge queue cannot distinguish "fast green, slow still running" from "all checks done". Required-status configuration would either wait forever on slow-tests or wait forever on a skipped job.

## Atomicity guarantee

The workflow declares a workflow-level concurrency group:

```yaml
concurrency:
  group: osdc-staging
  cancel-in-progress: false
```

The lock is held for the ENTIRE workflow run — fast AND slow. No other workflow that joins the `osdc-staging` group can touch staging mid-battery.

Trade-off: PR2's `deploy-fast` will not START until PR1's `slow-tests` finish. Queue throughput is bounded by the slow path.

## Required GitHub Settings configuration

This is the section operators MUST read.

### Required status checks

Repo Settings → Rules → "main" ruleset → Merge queue → Required status checks. The list MUST contain ONLY:

- `pre-merge-ok`

It MUST NOT contain individual job names like `Deploy meta-staging-aws-uw1`, `Smoke tests`, `Integration tests`, `Load tests`, `Compactor e2e tests`, `Janitor e2e tests`.

Why: those individual jobs may be SKIPPED (path filter excluded the PR) and never report. The merge queue would wait forever for a check that never arrives. The `pre-merge-ok` marker always runs and always reports.

### Status check timeout

Repo Settings → Rules → "main" ruleset → Merge queue → Status check timeout. MUST be raised from the default 60 minutes to at least 240 minutes (4 hours).

Why: the concurrency group `osdc-staging` is held for the entire battery. When PR2 enters the queue while PR1's slow-tests are still running, PR2's `deploy-fast` cannot start. PR2's required check `pre-merge-ok` stays pending. With the default 60-minute timeout, GitHub ejects PR2 from the queue before it ever gets a chance to run. Raising the timeout absorbs the worst-case wait.

### Build concurrency

Repo Settings → Rules → "main" ruleset → Merge queue → Build concurrency. Can stay at default. The workflow-level concurrency group serializes regardless.

## What to do when slow-tests fail

Slow-test failures DO NOT block merges (by design). They surface as a red check in the PR's Actions tab but the PR can still merge. Treat slow-test failures as a regression report for someone to investigate post-merge.

- If load-test fails: check that the canary GitHub token still works and that the runner controller is healthy in `meta-staging-aws-uw1`.
- If compactor fails: investigate node-compactor pod logs in `meta-staging-aws-uw1`.
- If janitor fails: investigate image-cache-janitor in `meta-staging-aws-uw1`.
- If drain/undrain fails: check the controller's canary PR/run URL, live `maxRunners`, listener `gha_capacity_advertised_max_runners`, and the recovery step. Missing listener metrics are failures, not evidence of zero capacity. If recovery failed, run `just drain-test meta-staging-aws-uw1 --restore-only` from `osdc/` with staging AWS access. This resumes and verifies configured limits and removal of refresh taints without creating canary jobs.

No notification automation today — see follow-up section.

## Path filter

The `changes` job uses `dorny/paths-filter` against these patterns:

- `osdc/**` (excluding test files under `tests/` and `*_test.py` / `test_*.py`)
- `.github/workflows/osdc-pre-merge.yml`
- `.github/workflows/osdc-deploy-prod.yml`
- `.github/workflows/_osdc-deploy.yml`
- `.github/workflows/_osdc-slow-tests.yml`

When none of these change, `deploy-fast` and `slow-tests` are skipped. `pre-merge-ok` reports success trivially.

## Manual re-run

To force a re-run of the full battery without a PR: dispatch `osdc-pre-merge.yml` via the Actions UI. `workflow_dispatch` forces `osdc: true` regardless of file changes, so both fast and slow run.

### Drain/undrain test

The controller runs on a GitHub-hosted runner and creates a unique temporary PR in `pytorch/pytorch-canary`. Only that branch's workflows are replaced. Probes are pinned to runner group `meta-staging-aws-uw1` and label `c-mt-l-x86iamx-8-16`; preflight rejects a disabled/missing scale set or a cluster whose live limits do not match `runner_max_map.py`.

One in-flight job waits for a unique marker in its PR body. The controller starts `just drain-runners` in the background, waits for all live runner limits and all listener advertised capacities to become zero, then releases that job. It must succeed normally. Three dependent jobs then queue; for 60 seconds the controller verifies that none is assigned a runner and that capacity remains zero. `just resume-runners` must restore the complete configured limit map, including region-excluded zeros, and remove refresh taints. All held jobs must subsequently succeed on the uw1 runner group.

The controller supplies its own queue/start and completion deadlines (30 minutes each), a drain-convergence deadline (10 minutes), and bounded CLI/API calls. The probes have no warm pool in uw1, and fresh-job sharding means even group-pinned jobs may need to age approximately 15 minutes before capacity is provisioned. Additional runtime consumes the shared `osdc-staging` lock; budget the normal start/completion waits rather than treating this as a quick smoke test. The CI test step is capped at 80 minutes, within a 90-minute job, with a separate 5-minute same-job recovery step.

Cleanup stops the drain operation before restoring and verifying runner limits, then cancels canary runs and removes the temporary PR/ref. Recovery is retried up to three times, and CI also attempts restoration in an `always()` step. Forced termination or runner loss can interrupt recovery; after cancellation, verify live runner limits and refresh taints, and use `just drain-test meta-staging-aws-uw1 --restore-only` if needed.

Offline checks (no AWS/Kubernetes/GitHub credentials or API calls):

```bash
cd osdc
uv run --frozen --offline pytest integration-tests/drain-test/scripts/python -v
just drain-test meta-staging-aws-uw1 --dry-run
```

The controller sets `OSDC_TAINT_NODES=no` for the drain recipe so a node taint cannot hide the historical zero-limit capacity-monitor bug. Recipes themselves are unchanged.

Live acceptance requires authorized staging access and the existing `CANARY_GITHUB_TOKEN` (canary contents/workflows and PR writes, Actions read/cancel):

1. Dispatch `osdc-pre-merge.yml` from the test branch; the drain/undrain slow test must pass.
2. Cancel a normal run after zero limits are observed, then verify all configured runner limits are restored and refresh taints removed. If the process/step is forcibly killed before cleanup completes, use `--restore-only` and record that the cancellation acceptance remains unmet.

A manual dispatch deploys the chosen branch to shared staging, holds the staging lock for the full battery, and can replace a pending merge-queue invocation. Coordinate live acceptance with maintainers; offline unit tests cannot establish it.

## Known trade-offs (already accepted)

- Queue throughput drops to ~1 PR per 3 hours under sustained load — direct cost of the atomicity guarantee.
- Slow-test failures produce no automated notification today. Future work could add a Slack ping or auto-issue on slow failure.
- The required-checks configuration lives in repo Settings, not in YAML. New repo admins must read this doc to set it up correctly.

## Files involved

| File | Role |
|------|------|
| `.github/workflows/osdc-pre-merge.yml` | Entry point, orchestrates fast + slow + marker job |
| `.github/workflows/_osdc-deploy.yml` | Reusable: deploy + smoke + integration |
| `.github/workflows/_osdc-slow-tests.yml` | Reusable: load-test + compactor + janitor + drain-test |
| `osdc/integration-tests/drain-test/` | Offline assertions/tests, real canary probe template and drain/undrain controller |
