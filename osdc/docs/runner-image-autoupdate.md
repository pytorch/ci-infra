# Runner Image Auto-Update Pipeline

## What this pipeline does

A Renovate-driven, unattended pipeline that keeps `runner_image_tag` in `osdc/clusters.yaml` current with new GitHub Actions runner image releases on GHCR. Runs on cron (Mon + Thu, 14:00 UTC) plus `workflow_dispatch`. When a release at least 5 days old is found, Renovate opens a single-line bump PR on a `renovate-runner/*` branch with the `auto-runner-update` label. A separate workflow auto-approves and enqueues the PR into the merge queue, which triggers full staging validation (deploy + smoke + integration + load test) via `osdc-pre-merge.yml`. On a green queue run the PR squash-merges; a post-merge workflow then deploys the new image to `arc-cbr-production-uw1` (smoke), then `arc-cbr-production` (smoke), sequentially.

## Prerequisites

The pipeline relies on the following repo configuration. If any is missing, the pipeline silently does nothing or fails the gate.

- **GitHub Environment `osdc-renovate`**
  - No required reviewers
  - No wait timer
  - Deployment-branch policy restricted to `main`
  - Holds both PATs below
- **Repo secret `UPDATEBOT_TOKEN`** — PAT owned by `pytorchupdatebot`. Used by `osdc-renovate.yml` to push `renovate-runner/*` branches and open PRs.
- **Repo secret `GH_PYTORCHBOT_TOKEN`** — PAT owned by `pytorchbot`. Used by `osdc-renovate-autoapprove.yml` to approve, enqueue, comment, and close PRs.
- **Distinct identities.** `UPDATEBOT_TOKEN` and `GH_PYTORCHBOT_TOKEN` MUST be owned by different GitHub accounts. The branch ruleset on `main` forbids self-approval — if both PATs are the same identity, every auto-approval is rejected by GitHub and nothing ever merges.
- **Repo variable `OSDC_RENOVATE_BOT_LOGIN`** — the GitHub login of `UPDATEBOT_TOKEN`'s owner. Today: `pytorchupdatebot`. Read by both the auto-approver and the post-merge deploy gate to verify the PR author matches. The discover job fails closed if this variable is empty.
- **Branch ruleset on `main`** — requires 1 approving review and forbids self-approval.
- **Merge queue enabled on `main`** with `osdc-pre-merge` configured as a **queue-required** check. It MUST be a queue-required check, not a plain branch-required check: `osdc-pre-merge.yml` only runs inside the merge queue, so listing it as a plain required check would block `gh pr merge --auto` from ever queueing the PR (the required check would be permanently pending). Without `osdc-pre-merge` in the queue, the PR auto-merges on approval alone with zero staging validation.

## Pause the pipeline

Remove the `auto-runner-update` label from any open Renovate PR. The auto-approve job and the stale-close job both filter on this label, so an unlabeled PR will not be approved, enqueued, or auto-closed. Renovate itself will continue to open new PRs on the next scheduled run; close them manually or unlabel them as they appear.

To stop Renovate from opening PRs at all, disable the `osdc-renovate.yml` workflow via the Actions UI.

## Manually trigger a check

Run `osdc-renovate.yml` via `workflow_dispatch` from the Actions UI. Inputs:

| Input | Type | Default | Effect |
|-------|------|---------|--------|
| `dryRun` | boolean | `false` | When true, Renovate logs what it would do without opening or updating PRs |
| `logLevel` | choice (`info`/`debug`) | `info` | Increase verbosity for diagnosing why a release is or is not being picked up |

Use `dryRun=true` with `logLevel=debug` to confirm Renovate sees a new release without producing a PR.

## Manually trigger a prod deploy

If an auto-update PR merged to `main` but `osdc-auto-update-deploy-prod.yml` did not fire (gate failure, infra error) or failed mid-rollout, dispatch `osdc-deploy-prod.yml` from the Actions UI. Inputs to use for a runner-image catch-up:

- `target`: `all` (or the specific cluster that failed)
- `taint_nodes`: usually `false` for a runner-image bump — new pods pick up the new image as listeners recycle; only set `true` if forcing a graceful node refresh
- `restart_listeners`: `false` (only required when the ARC controller image changes, not the runner image)
- `skip_lint_test`: `false`

`osdc-deploy-prod.yml` shares the same `osdc-deploy-prod` concurrency group as the auto-deploy workflow, so a manual run will queue behind any in-flight auto-deploy rather than racing it.

## Cross-cluster inconsistency recovery

The auto-deploy is sequential: `arc-cbr-production-uw1` first, then `arc-cbr-production` (ue2). If uw1 succeeds and ue2 fails, the fleet is left split — uw1 on the new runner image, ue2 on the old.

Recover by either:

1. **Re-run ue2 only.** Investigate the ue2 failure (check the workflow logs and smoke output). Once the underlying issue is fixed, dispatch `osdc-deploy-prod.yml` with `target=arc-cbr-production`. This brings ue2 up to the version uw1 is already on.
2. **Revert the bump.** Open a revert PR for the merge commit on `osdc/clusters.yaml`, merge through the normal flow. The next Renovate run will propose the next-available version — if the original version is the only candidate, Renovate will re-open the same bump and you must fix the ue2 issue before re-merging.

Do not leave the fleet split across clusters indefinitely — the auto-approver only validates one PR at a time and will keep proposing bumps assuming both clusters are on the merged version.

## Auto-close reasons

The auto-approver validates the PR diff before approving. Deterministic validation failures close the PR with a comment and a `close-<reason>` decision. Renovate re-opens on the next run when a new eligible version appears.

| Decision | Trigger | Operator action |
|----------|---------|-----------------|
| `close-wrong-file-count` | PR changes more than one file | Inspect the PR diff — Renovate config drift or a Renovate bug; check whether `osdc/renovate.json` was changed |
| `close-wrong-file` | The single changed file is not `osdc/clusters.yaml` | Same as above — verify `osdc/renovate.json` `matchFileNames` / `matchManagers` config |
| `close-no-patch` | GitHub returned no patch for the file (binary, rename-only, or oversized) | Inspect the PR on GitHub directly; likely a Renovate or GitHub API anomaly |
| `close-multi-line` | The diff is not exactly +1 / -1 lines | The bump touched more than the single `runner_image_tag` line — check `osdc/renovate.json` regex and the surrounding YAML |
| `close-bad-pattern` | The added or removed line does not match `^    runner_image_tag: "X.Y.Z"$` (with optional comment) | Either the YAML formatting in `clusters.yaml` was changed (indentation, quoting) or Renovate produced a non-semver tag — check both |
| `close-no-change` | Old and new version strings are identical | Renovate produced a no-op bump; usually transient — verify with a `dryRun` |
| `close-downgrade` | New version sorts lower than old version | Suspect a tampered token or a Renovate misconfiguration that picked a lower tag — investigate the token's last-used activity and the Renovate run logs before re-enabling |

Transient failures (gh API errors, network) propagate as job failures and leave the PR open for retry on the next schedule.

## Stale-close behavior

Any PR with the `auto-runner-update` label whose `updatedAt` is older than 7 days is auto-closed by `osdc-renovate-autoapprove.yml` with a comment explaining the close. Renovate re-opens on the next run if the version is still relevant. `updatedAt` (not `createdAt`) is used so PRs Renovate keeps rebasing do not get closed under the operator.

To keep a stuck PR alive past 7 days for investigation, remove the `auto-runner-update` label. The discover job filters on this label, so unlabeled PRs are not eligible for stale-close.

## Secret rotation

Both PATs live in the `osdc-renovate` GitHub Environment. To rotate:

| Secret | Owner | Required scopes | Used by |
|--------|-------|-----------------|---------|
| `UPDATEBOT_TOKEN` | `pytorchupdatebot` | Contents: Read+write; Pull requests: Read+write; Metadata: Read. **Must NOT have**: Workflows, Administration, Actions, Secrets, or any org permissions | `osdc-renovate.yml` (push branches, open PRs) |
| `GH_PYTORCHBOT_TOKEN` | `pytorchbot` | Contents: Read; Pull requests: Read+write; Metadata: Read. **Must NOT have**: Workflows, Administration, Actions, Secrets, or any org permissions | `osdc-renovate-autoapprove.yml` (approve, enqueue, comment, close) |

Rotation steps:

1. Mint the new PAT as the same GitHub account (do not change the owning identity — the auto-approver compares the PR author against `OSDC_RENOVATE_BOT_LOGIN`, and `pytorchbot` / `pytorchupdatebot` must remain distinct).
2. Update the secret value in repo Settings → Environments → `osdc-renovate`.
3. Revoke the old PAT from the owning account.
4. Trigger `osdc-renovate.yml` with `dryRun=true` to confirm the new PAT works end to end.

If the owning identity of `UPDATEBOT_TOKEN` ever changes, also update the repo variable `OSDC_RENOVATE_BOT_LOGIN` to the new login. If the owning identity of `GH_PYTORCHBOT_TOKEN` ever changes, verify it is still distinct from `OSDC_RENOVATE_BOT_LOGIN` or the self-approval ban will block every merge.

## Files involved

| File | Role |
|------|------|
| `osdc/renovate.json` | Renovate config — match rules for `runner_image_tag`, schedule, labels, branch prefix, minimum release age |
| `.github/workflows/osdc-renovate.yml` | Scheduled + dispatchable Renovate runner. Opens the bump PR |
| `.github/workflows/osdc-renovate-autoapprove.yml` | Triggered by `workflow_run` on the renovate workflow. Validates the diff, approves, enqueues, and stale-closes |
| `.github/workflows/osdc-auto-update-deploy-prod.yml` | Triggered by `push` to `main` touching `osdc/clusters.yaml`. Re-validates the merge commit, then deploys uw1 then ue2 sequentially |
| `.github/workflows/osdc-pre-merge.yml` | Merge-queue check — full staging deploy + smoke + integration + load test |
| `.github/workflows/osdc-deploy-prod.yml` | Manual prod deploy used to recover from a failed or skipped auto-deploy |
