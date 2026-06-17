# Runner Image Auto-Resolve

## What

Each `arc-runners` deploy pins the runner image
(`ghcr.io/actions/actions-runner:<tag>@sha256:<...>`) **per OSDC commit**.
The lookup key is the SHA of the most recent commit touching anything in
the OSDC project (the whole `osdc/` directory) — `modules/arc-runners/`,
the `arc-runners-h100`/`arc-runners-b200` shim modules, `clusters.yaml`,
`deploy.sh`, and everything else under `osdc/`. The first time a given
SHA deploys, the resolver calls GitHub `/releases/latest`, resolves the
digest with `crane`, and writes the entry to the `arc-runner-version-lock`
ConfigMap in `osdc-system`. Every subsequent deploy of the same SHA
returns the exact same `tag@digest` from the ConfigMap — no GitHub API
call, no `crane` call, no write.

## Why per-commit

Reproducibility for rollbacks **within the 20-SHA rolling window**.
`git checkout <old-sha> && just deploy-module <cluster> arc-runners`
reproduces the exact image that ran when that commit was originally
deployed, *as long as the SHA is still inside the window*. Pair with the
cluster's other module versions and you have a bit-for-bit rollback for
recent history.

The window is the last 20 OSDC SHAs (newest first). Rolling back to a
SHA that has aged out of the window will create a new entry with the
same SHA but potentially a different `tag@digest` — the original digest
is not recoverable from the lock once it ages out. The replay is still
safe (digest-pinned, recorded in the ConfigMap), but it is no longer
bit-for-bit identical to the original deploy.

Because the key is "any commit under `osdc/`", shim module changes
(`arc-runners-h100`, `arc-runners-b200`), `clusters.yaml` edits, and
unrelated module commits all bump the SHA and produce a new lock entry.
Reproducibility is per-OSDC-commit, not per-`arc-runners`-commit.

## ConfigMap shape

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: arc-runner-version-lock
  namespace: osdc-system
  labels:
    app.kubernetes.io/managed-by: osdc-deploy-log
    osdc.io/lock-kind: arc-runner-version
data:
  history.json: |
    [
      {"osdc_sha": "3c22361...", "tag": "2.335.0", "digest": "sha256:abc...", "resolved_at": "2026-06-15T17:42:11Z"},
      {"osdc_sha": "1a0f2e4...", "tag": "2.335.0", "digest": "sha256:abc...", "resolved_at": "2026-06-08T09:03:55Z"}
    ]
```

Two different SHAs sharing the same `tag@digest` is the common case
(most OSDC commits don't bump the upstream runner). The dedupe key is
`osdc_sha`, not `tag` — multiple entries with the same tag are
expected and correct.

Fields: `osdc_sha` (40-char hex, the cache key), `tag` (release tag with
leading `v` stripped), `digest` (`sha256:<64-hex>`, the OCI manifest-list
digest — kubelet picks the per-arch manifest at pull time), `resolved_at`
(ISO8601 UTC).

History is capped at 20, newest first. Re-resolving the same SHA drops
its older entry before prepending the new one.

## Concurrent deploys

The three `arc-runners*` modules (`arc-runners`, `arc-runners-h100`,
`arc-runners-b200`) deploy concurrently and all read/write the same
ConfigMap. Writes use Kubernetes optimistic concurrency: the resolver
reads `metadata.resourceVersion`, includes it in the `replace` call, and
the API server rejects the write with 409 if another deploy beat it.

On 409 the loser re-reads the ConfigMap. If the winner already pinned
this SHA, the loser returns the winner's `tag@digest` instead of writing
— so all three modules end up with the same image. If the winner pinned
a different SHA (none of the runners pinned ours), the loser retries the
write with the fresh `resourceVersion`. Up to 5 attempts before giving
up.

## Rollback paths

1. **Revert to an earlier OSDC commit.** `git checkout <old-sha> &&
   just deploy-module <cluster> arc-runners`. If `<old-sha>` is still
   inside the 20-entry window, the same `tag@digest` is replayed
   exactly. Outside the window, the resolver re-resolves to today's
   latest and writes a new entry (same `osdc_sha`, potentially
   different `tag@digest` than the original).

2. **Pin to a known-good tag.** Add `arc.runner_image_tag: "<tag>"`
   under the cluster's `arc:` block in `clusters.yaml`, then redeploy.
   The resolver is bypassed entirely: deployed image becomes
   `ghcr.io/actions/actions-runner:<tag>` (tag-only, no digest), no
   GitHub API call, no `crane` call, ConfigMap untouched. Removing
   the key on a later deploy returns the cluster to auto-resolution.

## Failure modes

Each of these aborts the deploy with a non-zero exit and a single-line
stderr message. Operator response is the same in every case: pin
`arc.runner_image_tag` in `clusters.yaml` and re-run the deploy, then
investigate before unpinning.

- `git log` returns no commit history under the OSDC root (shallow
  clone) — refetch full history or pin manually.
- GitHub `/releases/latest` HTTP error, timeout, or response missing
  `tag_name`.
- `crane digest` exits non-zero (ghcr unreachable, tag missing, or auth
  failure on the runner image).
- ConfigMap read returns malformed JSON or entries missing
  `osdc_sha`/`tag`/`digest`.
- ConfigMap write fails after 5 conflict-retry attempts, or fails with a
  non-409 error (RBAC, API outage).

## Why digest pinning

Tags on ghcr are mutable in principle — a re-push of the same tag would
silently shift what kubelet pulls on the next pod restart. Digests are
content-addressed and cannot be changed. The ConfigMap records exactly
what was deployed, and the pod manifest pulls exactly that.

## Why no Renovate bot

Deploy-time resolution avoids running and maintaining bot infrastructure
in this repo and avoids the PR-churn cycle for a value that is read
fresh on every deploy anyway. Trade-off: deploys depend on GitHub
releases and ghcr being reachable at deploy time (see Failure modes for
the operator response when they are not).

## Where to look

- `modules/arc-runners/scripts/python/resolve_runner_version.py` — the
  resolver implementation.
- `modules/arc-runners/deploy.sh` — invokes the resolver and exports
  `RUNNER_IMAGE` for the generator. The `arc-runners-h100` and
  `arc-runners-b200` variants `exec` into this `deploy.sh` and inherit
  the resolved image unchanged.
- `arc-runner-version-lock` ConfigMap in the `osdc-system` namespace —
  the lock file itself.
- `modules/arc-runners/tests/smoke/test_runner_version_lock.py` — smoke
  test that validates the ConfigMap shape and cross-checks the entry
  whose `osdc_sha` matches the current commit against the deployed
  `AutoscalingRunnerSet` pod templates.
