# Pre-merge CI

## What this is

The pre-merge CI runs the OSDC validation battery against the `meta-staging-aws-uw1` cluster when PRs enter the merge queue. It is the gate that determines whether a PR is allowed to merge into `main`.

## Workflow shape

The workflow is defined in `.github/workflows/osdc-pre-merge.yml` and triggers on `merge_group` and `workflow_dispatch`. It is composed of four jobs:

1. `changes` — uses `dorny/paths-filter` to detect whether the PR touches osdc-related files. Outputs `osdc: true|false`.
2. `deploy-fast` — calls reusable `_osdc-deploy.yml` (lint+test → deploy `meta-staging-aws-uw1` → smoke + integration tests). Runs only when osdc files changed.
3. `slow-tests` — calls reusable `_osdc-slow-tests.yml` (load-test → compactor + janitor). Runs after `deploy-fast` succeeds. Informational only.
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
| What it runs      | lint + test, deploy meta-staging-aws-uw1, smoke, integration | load-test, compactor e2e, janitor e2e |
| Gates merge?      | Yes (via `pre-merge-ok`)                   | No (informational only)              |
| Typical duration  | ~30-60 min                                 | ~2-3 h                               |
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

Why: the concurrency group `osdc-staging` is held for the entire battery (~2-3h worst case). When PR2 enters the queue while PR1's slow-tests are still running, PR2's `deploy-fast` cannot start. PR2's required check `pre-merge-ok` stays pending. With the default 60-minute timeout, GitHub ejects PR2 from the queue before it ever gets a chance to run. Raising the timeout absorbs the worst-case wait.

### Build concurrency

Repo Settings → Rules → "main" ruleset → Merge queue → Build concurrency. Can stay at default. The workflow-level concurrency group serializes regardless.

## What to do when slow-tests fail

Slow-test failures DO NOT block merges (by design). They surface as a red check in the PR's Actions tab but the PR can still merge. Treat slow-test failures as a regression report for someone to investigate post-merge.

- If load-test fails: check that the canary GitHub token still works and that the runner controller is healthy in `meta-staging-aws-uw1`.
- If compactor fails: investigate node-compactor pod logs in `meta-staging-aws-uw1`.
- If janitor fails: investigate image-cache-janitor in `meta-staging-aws-uw1`.

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

## Known trade-offs (already accepted)

- Queue throughput drops to ~1 PR per 3 hours under sustained load — direct cost of the atomicity guarantee.
- Slow-test failures produce no automated notification today. Future work could add a Slack ping or auto-issue on slow failure.
- The required-checks configuration lives in repo Settings, not in YAML. New repo admins must read this doc to set it up correctly.

## Files involved

| File | Role |
|------|------|
| `.github/workflows/osdc-pre-merge.yml` | Entry point, orchestrates fast + slow + marker job |
| `.github/workflows/_osdc-deploy.yml` | Reusable: deploy + smoke + integration |
| `.github/workflows/_osdc-slow-tests.yml` | Reusable: load-test + compactor + janitor |
