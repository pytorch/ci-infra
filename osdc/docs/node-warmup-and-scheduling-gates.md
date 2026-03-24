# Node Warm-Up and Scheduling Gates

## Overview

When Karpenter provisions a new runner node, several initialization steps must complete before the node can accept GitHub Actions workflow jobs. These steps are orchestrated through Kubernetes-native mechanisms — startup taints, DaemonSets, and init containers — with no reliance on EC2 userdata or bootstrap scripts.

The system guarantees that every runner node has a warm git cache, patched runner-container-hooks, containerd registry mirrors, and optimized CPU/GPU settings before any workflow job is dispatched to it.

## Full Startup Timeline

```
Node provisioned by Karpenter
│
├─ startupTaint applied: git-cache-not-ready=true:NoSchedule
│  (blocks runner pods from scheduling on this node)
│
├─ DaemonSets schedule immediately (tolerate all taints):
│  ├─ git-cache-warmer         → rsyncs git repos from central pod to local NVMe
│  ├─ runner-hooks-warmer      → downloads patched runner-container-hooks
│  ├─ registry-mirror-config   → configures containerd to pull through Harbor
│  └─ node-performance-tuning  → CPU governor, kubelet tuning, GPU persistence
│
├─ git-cache-warmer removes startupTaint after successful sync
│  → runner pods can now schedule on this node
│
└─ Runner pod starts:
   └─ initContainer: wait-for-hooks
      polls /opt/runner-hooks/dist/index.js every 10s (timeout 300s)
      └─ Runner container starts → registers with GitHub → picks up job
         └─ runner-container-hooks create job pod (15 min timeout)
```

## Scheduling Gates

### Gate 1: Git Cache (Startup Taint)

**Mechanism**: Karpenter startup taint (`git-cache-not-ready=true:NoSchedule`)

Every Karpenter NodePool applies this startup taint via the `startupTaints` field in the NodePool spec. Runner pods tolerate this taint but job pods do not — the taint prevents premature scheduling.

**Defined in**: `modules/nodepools/scripts/python/generate_nodepools.py` (hardcoded in the template for every generated NodePool)

**Removed by**: `git-cache-warmer` DaemonSet (`base/kubernetes/git-cache/`)

**Flow**:
1. DaemonSet pod starts on the new node (tolerates the taint)
2. Waits for the central git-cache pod's rsyncd to accept TCP connections (600s timeout)
3. Rsyncs bare git repos from central pod to local NVMe at `/mnt/git-cache` using dual-slot rotation (cache-a / cache-b with atomic symlink swap)
4. On successful initial sync, removes the taint via Kubernetes API JSON Patch (RFC 6902 `test` + `remove` — atomic, race-safe)
5. Enters periodic refresh loop (every 300s)

**If sync fails**: Retries with exponential backoff (30s, 60s, 120s... capped at 300s). The node stays tainted indefinitely until a successful sync. Runner pods will not schedule.

**RBAC**: The `git-cache-warmer` ServiceAccount has `get` + `patch` on `nodes` (`base/kubernetes/git-cache/rbac.yaml`).

### Gate 2: Patched Hooks (Init Container Poll)

**Mechanism**: `wait-for-hooks` init container in every runner pod

Runner pods include an init container that polls for a file on the host filesystem before the runner container can start. This file is placed by a DaemonSet.

**DaemonSet**: `runner-hooks-warmer` (`modules/arc-runners/kubernetes/hooks-warmer.yaml`)
- Downloads patched `runner-container-hooks` v0.8.3 from GitHub releases
- Extracts to `/mnt/runner-container-hooks/dist/` on host NVMe
- Validates `dist/index.js` exists after extraction
- Writes version marker for idempotency
- Runs on nodes with `workload-type: github-runner`

**Init container**: `wait-for-hooks` (`modules/arc-runners/templates/runner.yaml.tpl`)
- Polls `/opt/runner-hooks/dist/index.js` every 10 seconds
- Timeout: 300 seconds (5 minutes) — pod fails on timeout
- Volume: hostPath `/mnt/runner-container-hooks` mounted read-only at `/opt/runner-hooks`

**Why patched hooks**: The upstream `actions/runner-container-hooks` has a performance bug (`find -exec stat {} \;` instead of `find -exec stat {} +`) causing 10-100x slower workspace initialization. The patched version fixes this. This gate is temporary — remove when upstream merges the fix.

## Node-Compactor Interaction

The node-compactor (`base/node-compactor/`) can taint nodes with `node-compactor.osdc.io/consolidating=true:NoSchedule` when they are underutilized. Neither runner nor workflow pods tolerate this taint, so a compactor-tainted node is effectively dead for new scheduling.

**Protection for fresh nodes**: The compactor has a `min_node_age` grace period (default: 900 seconds / 15 minutes, configured via `node_compactor.min_node_age_seconds` in `clusters.yaml`). Nodes younger than this threshold are:
1. Skipped entirely during compaction evaluation
2. Forcibly untainted if somehow already tainted

This ensures freshly provisioned nodes complete their full warm-up sequence (git cache sync ~112s, hooks download, registry config) and have time to run at least one job before the compactor can consider them for consolidation.

**Why 15 minutes**: The startup taint (`git-cache-not-ready`) is removed after ~112 seconds. The runner pod then starts in ~20-30 seconds and creates a workflow pod. A 15-minute grace period provides ample margin for the full startup-to-job-completion cycle, preventing the compactor from tainting nodes that are still serving their first job.

**Failure mode without sufficient grace period**: If `min_node_age` is too short, the compactor can taint nodes while they are still in the warm-up window or running their first job. When combined with the Karpenter startup-taint deadlock (Karpenter ignores startup taints in provisioning decisions), this can leave workflow pods with no schedulable node — causing "backoff timeout" failures.

## Non-Gating DaemonSets

These DaemonSets also run on new nodes but do not block runner scheduling:

### NVIDIA Device Plugin (GPU nodes only)

**File**: `base/kubernetes/nvidia-device-plugin.yaml`

Exposes `nvidia.com/gpu` resources to the Kubernetes scheduler. Without this DaemonSet, GPU workloads cannot request GPU resources and will not schedule. Runs only on nodes with label `nvidia.com/gpu: "true"`. Tolerates `git-cache-not-ready` so it starts during the warm-up period.

### Registry Mirror Config

**File**: `base/kubernetes/registry-mirror-config.yaml`

Configures containerd's `certs.d/` on every node to route image pulls through Harbor's proxy cache at `localhost:30002`. Covers six registries: docker.io, ghcr.io, public.ecr.aws, nvcr.io, registry.k8s.io, quay.io. Uses a marker file for idempotency. If Harbor is unavailable, containerd falls through to upstream registries automatically.

Runs on ALL nodes (no nodeSelector, tolerates everything).

### Node Performance Tuning

**File**: `base/kubernetes/node-performance-tuning.yaml`

Runs a privileged init container (`tune-node`) that configures:
- CPU governor set to `performance`
- Kubelet static CPU manager policy verification
- Topology manager policy verification
- NVIDIA GPU persistence mode (GPU nodes only)

Runs on nodes with `workload-type` in `[github-runner, buildkit]`.

## Taint Summary

| Taint Key | Type | Effect | Scope | Removed By |
|-----------|------|--------|-------|------------|
| `git-cache-not-ready=true` | Startup taint | `NoSchedule` | All Karpenter NodePools | git-cache-warmer DaemonSet |
| `instance-type={type}` | Permanent | `NoSchedule` | All Karpenter NodePools | Never (scheduling constraint) |
| `nvidia.com/gpu=true` | Permanent | `NoSchedule` | GPU NodePools only | Never (scheduling constraint) |
| `node-compactor.osdc.io/consolidating=true` | Runtime (dynamic) | `NoSchedule` | Applied by node-compactor | node-compactor controller (protected by `min_node_age`: 900s) |
| `CriticalAddonsOnly=true` | Permanent | `NoSchedule` | Base infrastructure nodes (EKS-managed) | Never |

## Toleration Pattern

All DaemonSets targeting runner/buildkit nodes use a consistent toleration set:

```yaml
tolerations:
  - key: instance-type
    operator: Exists
    effect: NoSchedule
  - key: nvidia.com/gpu
    operator: Exists
    effect: NoSchedule
  - key: git-cache-not-ready
    operator: Exists
    effect: NoSchedule
  - key: cpu-type
    operator: Exists
    effect: NoSchedule
  - key: karpenter.sh/unschedulable
    operator: Exists
    effect: NoSchedule
  - key: CriticalAddonsOnly
    operator: Exists
    effect: NoSchedule
  # Plus standard node condition taints
```

This ensures DaemonSet pods schedule on nodes immediately at provisioning time, before any startup taints are removed.

## Key Files

| File | Role |
|------|------|
| `modules/nodepools/scripts/python/generate_nodepools.py` | Defines `git-cache-not-ready` startup taint for all NodePools |
| `base/kubernetes/git-cache/daemonset.yaml` | Git-cache-warmer DaemonSet spec |
| `base/kubernetes/git-cache/daemonset-configmap.yaml` | `daemonset.py` — sync logic, taint removal, metrics |
| `base/kubernetes/git-cache/rbac.yaml` | RBAC for taint removal (get + patch nodes) |
| `modules/arc-runners/kubernetes/hooks-warmer.yaml` | Hooks-warmer DaemonSet (downloads patched hooks) |
| `modules/arc-runners/templates/runner.yaml.tpl` | Runner pod template with `wait-for-hooks` init container |
| `base/kubernetes/registry-mirror-config.yaml` | Containerd registry mirror DaemonSet |
| `base/kubernetes/node-performance-tuning.yaml` | CPU/GPU tuning DaemonSet |
| `modules/arc-runners/scripts/python/validate_runner_qos.py` | Deploy-time validation (checks hooks init container exists) |
