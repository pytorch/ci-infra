# Runner Image Auto-Resolve

## What

Each `arc-runners` deploy resolves the latest `actions/runner` GitHub release
to a digest-pinned image (`ghcr.io/actions/actions-runner:<tag>@sha256:<...>`)
and writes the result to the `arc-runner-version-lock` ConfigMap in
`osdc-system`. The deployed `AutoscalingRunnerSet` pod templates then pull
exactly that digest. Rollback is a single key in `clusters.yaml`.

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
      {"tag": "2.335.0", "digest": "sha256:abc...", "resolved_at": "2026-06-15T17:42:11Z"},
      {"tag": "2.334.0", "digest": "sha256:def...", "resolved_at": "2026-06-08T09:03:55Z"}
    ]
```

Each entry: `tag` (release tag with leading `v` stripped), `digest`
(`sha256:<64-hex>`, the OCI manifest-list digest — kubelet picks the per-arch
manifest at pull time), `resolved_at` (ISO8601 UTC).

History is capped at 20 entries, newest first. When a tag is resolved again,
any older entry with the same tag is dropped before the new entry is
prepended (no duplicate tags in the list).

## Rollback paths

1. **Pin to a known-good tag.** Add `arc.runner_image_tag: "<tag>"` under
   the cluster's `arc:` block in `clusters.yaml`, then
   `just deploy-module <cluster> arc-runners`. The resolver is bypassed
   entirely: deployed image becomes `ghcr.io/actions/actions-runner:<tag>`
   (tag-only, no digest), no GitHub API call, no `crane` call, ConfigMap
   untouched. Removing the key on a later deploy returns the cluster to
   auto-resolution.

2. **Revert the OSDC commit.** Use when a particular OSDC change drove the
   regression rather than the runner image itself. Note: reverting OSDC does
   not change which runner version gets picked — if the same upstream tag is
   still `latest`, the resolver re-resolves to it. Pair with option 1 if you
   also need to roll the runner version back.

## Failure modes

Each of these aborts the deploy with a non-zero exit and a single-line
stderr message. Operator response is the same in every case: pin
`arc.runner_image_tag` in `clusters.yaml` and re-run the deploy, then
investigate before unpinning.

- GitHub `/releases/latest` HTTP error, timeout, or response missing
  `tag_name`.
- `crane digest` exits non-zero (ghcr unreachable, tag missing, or auth
  failure on the runner image).
- ConfigMap read returns malformed JSON in `history.json`.
- ConfigMap write fails (RBAC, API outage, conflict).

## Why digest pinning

Tags on ghcr are mutable in principle — a re-push of the same tag would
silently shift what kubelet pulls on the next pod restart. Digests are
content-addressed and cannot be changed. The ConfigMap records exactly what
was deployed, and the pod manifest pulls exactly that.

## Why no Renovate bot

Deploy-time resolution avoids running and maintaining bot infrastructure
in this repo and avoids the PR-churn cycle for a value that is read fresh
on every deploy anyway. Trade-off: deploys depend on GitHub releases and
ghcr being reachable at deploy time (see Failure modes for the
operator response when they are not).

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
  test that validates the ConfigMap shape and cross-checks the newest
  entry against the deployed `AutoscalingRunnerSet` pod templates.
