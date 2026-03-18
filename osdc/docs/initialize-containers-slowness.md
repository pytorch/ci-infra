# "Initialize Containers" Step Slowness on OSDC Runners

## Summary

The "Initialize containers" step on `l-x86iamx-8-16` OSDC runners takes 17-50+ minutes. The bottleneck is **not** image pulls — it's the ARC `k8s-novolume` hook's workspace copy verification loop running on a CPU-starved runner pod.

## Investigation

**Example job**: [pytorch/pytorch #23222770828 / job 67498880233](https://github.com/pytorch/pytorch/actions/runs/23222770828/job/67498880233)

- **Workflow**: `Lint (unstable)` (`lint-osdc.yml`)
- **Job**: `quick-checks / lint`
- **Runner**: `l-x86iamx-8-16` (ARC self-hosted, `pytorch-arc-cbr-production`, us-east-2)
- **Container mode**: `kubernetes-novolume` (ARC creates a separate Kubernetes pod per workflow job)
- **Job container image**: `ghcr.io/pytorch/test-infra:cpu-x86_64-3954e68` (690 MB, 27 layers)

## Time Breakdown

| Phase | Duration | Notes |
|-------|----------|-------|
| Pod scheduling | < 1 second | Node already exists |
| Image pull: `actions-runner:latest` | 276 ms | Cached on node (515 MB) |
| Image pull: `test-infra:cpu-x86_64-*` | ~24 seconds | Via Harbor pull-through cache (690 MB) |
| Workspace tar + copy | ~1-2 min | 258 MB via `kubectl exec` websocket |
| **Verification loop** | **~47 min** | **The bottleneck** |

## Root Cause

The `k8s-novolume/index.js` hook (bundled in the ARC runner container) performs these steps during "Initialize containers":

1. Creates the workflow pod with the job container
2. Waits for the pod to reach Running phase
3. Tars the runner workspace (`/home/runner/_work`, 258 MB, 25,781 files) and streams it to the workflow pod via `kubectl exec`
4. **Verifies the copy** by running `find . -exec stat -c '%s %n' {} \;` on BOTH pods, sorting, SHA-256 hashing, and comparing — retries up to 15 times on mismatch

Three compounding problems make step 4 catastrophically slow:

### 1. Per-file process fork (`\;` instead of `+`)

The verification command uses:

```bash
find . -exec stat -c '%s %n' {} \;
```

This forks a new `stat` process for each of 25,781 files. Using `find -exec stat {} +` (batched execution) would be 10-100x faster.

### 2. Runner pod has only 200m CPU

The runner pod (`runner.yaml.tpl`) requests/limits 200m CPU (1/5 of a core). At this budget, a single `find` invocation across 25,781 files takes ~10-15 minutes instead of seconds at full CPU.

### 3. Double execution + retry loop

The verification runs the `find` command **twice per attempt** (once on the runner pod, once on the workflow pod via `kubectl exec`). If hashes don't match (timing differences, files created during the find), it retries. With 3+ attempts at ~15 min each, total time reaches 47+ minutes.

## Scope

This affects **every** `l-x86iamx-8-16` job, not just this run. The CUDA image variant (`lintrunner-clang`, 20.7 GB) takes only ~8 minutes longer (58 vs 50 min), confirming image pull is not the bottleneck.

## Workspace Contents

The 258 MB / 25,781 files are dominated by pre-downloaded GitHub Actions:

- `_actions/pytorch/pytorch` — 218 MB alone
- These are downloaded by the runner before the hook runs, then copied into the workflow pod

## Potential Fixes

### 1. Increase runner pod CPU (simplest)

Bump CPU from 200m to 500m-1000m in `modules/arc-runners/templates/runner.yaml.tpl`. Makes the `find` command run in seconds instead of minutes.

**Trade-off**: uses more CPU on the node for the runner sidecar pod, reducing what's available for the workflow pod.

### 2. Upstream ARC fix (best long-term)

Change `find -exec stat {} \;` to `find -exec stat {} +` in the `k8s-novolume` hook. This is an ARC bug/inefficiency — batched execution would be 10-100x faster regardless of CPU budget.

### 3. Reduce workspace file count

The 218 MB `pytorch/pytorch` action contributes most files. If the action doesn't need to be pre-downloaded before the workflow pod starts, skipping it would dramatically cut file count and verification time.

## Key Files

| File | Role |
|------|------|
| `modules/arc-runners/templates/runner.yaml.tpl` | Runner pod definition (200m CPU at line 124) |
| `modules/arc-runners/defs/*.yaml` | Runner scale set definitions |
| `/home/runner/k8s-novolume/index.js` (in-container) | ARC hook that runs the copy + verification |

## Resolution

The following fixes have been deployed:

- **Runner pod CPU bumped from 200m to 750m** in `templates/runner.yaml.tpl`, giving the runner pod enough CPU to run `find`/`stat` verification in seconds instead of minutes.
- **Forked `runner-container-hooks`** to fix `find -exec stat {} \;` to `find -exec stat {} +` (batched execution, 10-100x faster). Fork released as [v0.8.1](https://github.com/jeanschmidt/runner-container-hooks/releases/tag/v0.8.1).
- **Hooks-warmer DaemonSet** (`kubernetes/hooks-warmer.yaml`) downloads the patched hooks once per node to NVMe at `/mnt/runner-container-hooks/`.
- **Runner pods override baked-in hooks** via `ACTIONS_RUNNER_CONTAINER_HOOKS` env var pointing to the patched version on the node.
- **`wait-for-hooks` init container** gates runner startup until the patched hooks are available on the node.

These fixes are **temporary** — remove them when upstream `actions/runner-container-hooks` merges the `find +` fix.

## Date

Investigated 2026-03-17.
