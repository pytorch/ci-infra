# Node Warm-Up and Scheduling Gates

## Overview

When Karpenter provisions a new runner node, several initialization steps must complete before the node can accept GitHub Actions workflow jobs. These steps are orchestrated primarily through Kubernetes-native mechanisms — startup taints, DaemonSets, and init containers. The standard nodepools (g4dn, g5, g6, p4d, c7i, c7a, m8g, etc.) require no per-node EC2 userdata scripts; the H100 (p5.48xlarge) and B200 (p6-b200.48xlarge) pools are the exception — both ship a per-fleet bash script referenced by `user_data_script:` in their def (`modules/nodepools-h100/scripts/h100-node-setup.sh`, `modules/nodepools-b200/scripts/b200-node-setup.sh`) that resolves the node's primary IPv6 from IMDS and writes the `<NODE_IPV6> harbor` /etc/hosts entry from cloud-init, before the registry-mirror-config DaemonSet would otherwise do it.

The cluster is end-to-end IPv6-only for pod networking (EKS `ip_family = "ipv6"`, kube-proxy IPv6, NLD binds the IPv6 ULA `fd00::10`, kube-dns ClusterIP is IPv6) — many of the conventions below (per-node /etc/hosts rewrites for `harbor:30002`, NLD bind addresses, `case "$KUBE_DNS_CLUSTER_IP" in *:*` validation) follow from that.

The system guarantees that every runner node has a warm git cache, patched runner-container-hooks, containerd registry mirrors, and optimized CPU/GPU settings before any workflow job is dispatched to it.

## Full Startup Timeline

```
Node provisioned by Karpenter
│
├─ startupTaint applied: git-cache-not-ready=true:NoSchedule
│  (blocks runner pods from scheduling on this node)
│
├─ DaemonSets schedule immediately (tolerate all taints):
│  ├─ nodelocaldns             → per-node CoreDNS cache (system-node-critical priority)
│  │                            (ALL nodes; image lazy-pulled via Harbor proxy cache —
│  │                             ~30-60s ImagePullBackOff race on fresh nodes until
│  │                             registry-mirror-config writes containerd hosts.toml)
│  ├─ git-cache-warmer         → rsyncs git repos from a central git-cache replica to local NVMe
│  │                            (only on workload-type ∈ [github-runner, buildkit])
│  ├─ runner-hooks-warmer      → downloads patched runner-container-hooks
│  │                            (only on node-fleet=c7i-runner — the dedicated runner pool)
│  ├─ registry-mirror-config   → configures containerd to pull through Harbor (ALL nodes)
│  └─ node-performance-tuning  → CPU governor, kubelet tuning, GPU persistence
│                                (only on workload-type ∈ [github-runner, buildkit])
│
├─ git-cache-warmer removes startupTaint after successful sync
│  → runner pods can now schedule on this node
│
└─ Runner pod starts:
   └─ initContainer: wait-for-hooks
      polls /mnt/host-hooks/dist/index.js every 10s (timeout 300s),
      then snapshots /mnt/host-hooks/dist/ → /opt/runner-hooks/dist/ (emptyDir)
      └─ Runner container starts → registers with GitHub → picks up job
         └─ OSDC wrapper.js → runner-container-hooks create job pod (25 min timeout)
```

## Scheduling Gates

### Gate 1: Git Cache (Startup Taint)

**Mechanism**: Karpenter startup taint (`git-cache-not-ready=true:NoSchedule`)

Every Karpenter NodePool applies this startup taint via the `startupTaints` field in the NodePool spec. Runner pods tolerate this taint but job pods do not — the taint prevents premature scheduling.

**Defined in**: `modules/nodepools/scripts/python/generate_nodepools.py` (hardcoded in the template for every generated NodePool)

**Removed by**: `git-cache-warmer` DaemonSet (`base/kubernetes/git-cache/`)

**Flow**:
1. DaemonSet pod starts on the new node (tolerates the taint)
2. Waits for the central git-cache ClusterIP Service (`git-cache-central.kube-system.svc.cluster.local`, port 873) to accept TCP connections (600s timeout) via `wait_for_central()`
3. Migrates any pre-existing single-directory cache layout into slot-a (one-time, if `/mnt/git-cache` exists as a plain directory)
4. Runs the initial sync: `rsync_from_central()` resolves the headless Service DNS for the central StatefulSet (multi-replica — `central_replicas: 15` for arc-cbr-production, cluster default `2`, configured via `git_cache.central_replicas` in `clusters.yaml`), queries each replica's metrics endpoint, picks the one with the fewest active connections, and rsyncs bare git repos from that replica to local NVMe at `/mnt/git-cache` using dual-slot rotation (cache-a / cache-b with atomic symlink swap)
5. On successful initial sync, removes the taint via Kubernetes API JSON Patch (RFC 6902 `test` + `remove` — atomic, race-safe)
6. Enters periodic refresh loop (every 300s — `FETCH_INTERVAL`), re-running `rsync_from_central()` (replica discovery happens fresh each cycle)

**If sync fails**: Retries with exponential backoff (30s, 60s, 120s... capped at 300s). The node stays tainted indefinitely until a successful sync. Runner pods will not schedule.

**RBAC**: The `git-cache-warmer` ServiceAccount has `get` + `patch` on `nodes` (`base/kubernetes/git-cache/rbac.yaml`).

**c7i-runner note**: The git-cache-warmer's nodeAffinity selects `workload-type In [github-runner, buildkit]`, and c7i-runner NodePools label nodes `workload-type: github-runner`. The warmer therefore schedules on c7i-runner nodes and removes the `git-cache-not-ready` taint there, even though runner pods on c7i-runner do not consume the `/mnt/git-cache` mount. The runner pod's explicit toleration on `git-cache-not-ready` (in `modules/arc-runners/templates/runner.yaml.tpl`) is belt-and-suspenders: it allows runner pods to schedule during the brief warmup window before the warmer clears the startup taint. A code comment in `runner.yaml.tpl` claims the warmer does NOT run on c7i-runner — that comment is contradicted by the actual nodeAffinity selector and should be treated as stale.

### Gate 2: Patched Hooks (Init Container Poll)

**Mechanism**: `wait-for-hooks` init container in every runner pod

Runner pods include an init container that polls for a file on the host filesystem before the runner container can start. This file is placed by a DaemonSet.

**DaemonSet**: `runner-hooks-warmer` (`modules/arc-runners/kubernetes/hooks-warmer.yaml`)
- Downloads patched `runner-container-hooks` v0.8.13 from GitHub releases
- Extracts to `/mnt/runner-container-hooks/dist/` on host NVMe
- Validates `dist/index.js` exists after extraction
- Writes version marker for idempotency
- Pinned via `nodeSelector: node-fleet: c7i-runner` — runs only on the dedicated c7i-runner pool, where runner pods live. Workflow-pool nodes and GPU runner pools (g4dn/g5/g6/p4d/p5/p6) do NOT get this DaemonSet because runner pods never schedule there.

**Init container**: `wait-for-hooks` (`modules/arc-runners/templates/runner.yaml.tpl`)
- Polls `/mnt/host-hooks/dist/index.js` every 10 seconds
- Timeout: 300 seconds (5 minutes) — pod fails on timeout
- Two-volume snapshot pattern:
  - `patched-hooks` — hostPath `/mnt/runner-container-hooks`, mounted read-only at `/mnt/host-hooks` (init container only)
  - `hooks-snapshot` — emptyDir (50Mi limit), mounted RW at `/opt/runner-hooks` for the init container, then RO at `/opt/runner-hooks` for the runner container
- After polling succeeds, the init container `cp -a /mnt/host-hooks/dist/ /opt/runner-hooks/dist/` (and copies the `.version` marker), then verifies `index.js` exists in the snapshot. The runner container only ever sees the emptyDir snapshot — it does not touch the hostPath directly.

**Wrapper indirection**: The runner's `ACTIONS_RUNNER_CONTAINER_HOOKS` env var points to `/home/runner/hook-extensions/wrapper.js` (the OSDC wrapper, mounted from a ConfigMap), NOT directly at `/opt/runner-hooks/dist/index.js`. The wrapper validates env vars (rate-limit responses, HTML error pages) and surfaces clearer exit codes, then spawns the real hooks at `/opt/runner-hooks/dist/index.js`.

**Why patched hooks**: The upstream `actions/runner-container-hooks` has a performance bug (`find -exec stat {} \;` instead of `find -exec stat {} +`) causing 10-100x slower workspace initialization. The patched version fixes this. This gate is temporary — remove when upstream merges the fix.

## Node-Compactor Interaction

The node-compactor (`base/node-compactor/`) can taint nodes with `node-compactor.osdc.io/consolidating=true:NoSchedule` when they are underutilized. Neither runner nor workflow pods tolerate this taint, so a compactor-tainted node is effectively dead for new scheduling.

**Protection for fresh nodes**: The compactor has a `min_node_age` grace period set per-cluster via `node_compactor.min_node_age_seconds` in `clusters.yaml` (the deploy script in `base/node-compactor/deploy.sh` falls back to `900` seconds / 15 minutes if unset). Nodes younger than this threshold are:
1. Skipped entirely during compaction evaluation
2. Forcibly untainted if somehow already tainted

This ensures freshly provisioned nodes complete their full warm-up sequence (git cache sync, hooks download, registry config) and have time to run at least one job before the compactor can consider them for consolidation.

**Why 15 minutes**: The startup taint (`git-cache-not-ready`) is removed once the initial git-cache sync completes — typically on the order of a couple of minutes, depending on cache size and central-replica load. The runner pod then starts and creates a workflow pod. A 15-minute grace period provides ample margin for the full startup-to-job-completion cycle, preventing the compactor from tainting nodes that are still serving their first job.

**Failure mode without sufficient grace period**: If `min_node_age` is too short, the compactor can taint nodes while they are still in the warm-up window or running their first job. When combined with the Karpenter startup-taint deadlock (Karpenter ignores startup taints in provisioning decisions), this can leave workflow pods with no schedulable node — causing "backoff timeout" failures.

## Non-Gating DaemonSets

These DaemonSets also run on new nodes but do not block runner scheduling:

### NodeLocal DNSCache (NLD)

**File**: `base/kubernetes/nodelocaldns/` (deployed via `base/kubernetes/nodelocaldns/deploy.sh`, invoked from the `deploy-base` just recipe — last step, after `image-cache-janitor`)

Per-node CoreDNS cache running as a DaemonSet on **every** node. Reduces cluster-wide DNS load and lowers per-pod resolution latency. Pod spec uses `priorityClassName: system-node-critical` so it lands early and won't be evicted under kubelet pressure. Resource footprint is `25m` CPU / `100Mi` memory requests, no limits (memory limit risks OOMKill → orphan iptables → cluster-wide DNS degradation).

**Why no startup taint blocks workloads on NLD readiness**: NLD operates in iptables-mode. The kubelet's `--cluster-dns` value is **unchanged** — pods continue resolving via the kube-dns Service ClusterIP. NLD installs NOTRACK iptables rules on a dummy `nodelocaldns` interface that binds both `fd00::10` (IPv6 ULA) and the kube-dns Service ClusterIP. While the NLD pod is not yet ready (or fails), DNS queries fall through gracefully to cluster CoreDNS via the unchanged kube-dns Service Endpoints. There is no scheduling deadlock to protect against, so no startup taint is needed.

**Cold-pull race on fresh Karpenter nodes**: The image (`registry.k8s.io/dns/k8s-dns-node-cache:1.26.8`, ~50MB) is lazy-pulled via the Harbor proxy cache — it is **not** pre-mirrored to ECR. On a brand-new node, NLD typically sees ~30-60s of `ImagePullBackOff` until `registry-mirror-config` writes containerd's `hosts.toml` and the proxy-cache pull succeeds. During that window, pods on the node continue to use cluster CoreDNS via the unchanged kube-dns Service — no pod-visible failure, just no per-node cache yet.

**Tolerations**: explicit list (NOT `operator: Exists`) — `CriticalAddonsOnly`, `nvidia.com/gpu`, `node-fleet`, `instance-type`, `git-cache-not-ready`. Covers all standard runner/buildkit/GPU taints so it schedules at provisioning time.

**Deploy ordering — Service BEFORE DaemonSet** (`base/kubernetes/nodelocaldns/deploy.sh`): kubelet snapshots Service env into pods at pod-create time, so the `kube-dns-upstream` Service must exist before the DaemonSet pods are scheduled or the binary will not see `KUBE_DNS_UPSTREAM_SERVICE_HOST/PORT`. The deploy script enforces this in seven steps: (1) verify any existing `kube-dns-upstream` Service has the correct `k8s-app=kube-dns` selector (refuses to overwrite a foreign one), (2) resolve the live kube-dns ClusterIP and fail-fast if it is IPv4 (NLD is IPv6-only on OSDC), (3) render manifests with `kubectl kustomize` and sed-substitute the `__KUBE_DNS_CLUSTER_IP__` placeholder, (4) split into a Services file and the rest, (5) `kubectl apply` Services first, (6) sleep 3s for ClusterIP allocation, (7) `kubectl apply` ConfigMap + ServiceAccount + DaemonSet. Then if the upstream Service was freshly created in THIS run (first-time deploy or out-of-band deletion + redeploy), the script runs `kubectl rollout restart ds node-local-dns` so pods re-pick the Service env on the next pod-create.

**Two metrics ports**: NLD exposes both `metrics` on port 9253 (CoreDNS plugin metrics — cache hits, forward latency, etc.) and `metrics-binary` on port 9353 (k8s-dns-node-cache binary metrics — setup errors, `coredns_nodecache_*`). The headless `node-local-dns-metrics` Service publishes both ports; both must be scraped to see the full picture.

### NVIDIA Device Plugin (GPU nodes only)

**File**: `base/kubernetes/nvidia-device-plugin.yaml`

Exposes `nvidia.com/gpu` resources to the Kubernetes scheduler. Without this DaemonSet, GPU workloads cannot request GPU resources and will not schedule. Runs only on nodes with label `nvidia.com/gpu: "true"`. Tolerates `git-cache-not-ready` so it starts during the warm-up period.

### Registry Mirror Config

**File**: `base/kubernetes/registry-mirror-config.yaml`

Configures containerd's `certs.d/` on every node to route image pulls through Harbor's proxy cache at `harbor:30002`. Covers six registries: docker.io, ghcr.io, public.ecr.aws, nvcr.io, registry.k8s.io, quay.io. Uses a marker file for idempotency. If Harbor is unavailable, containerd falls through to upstream registries automatically.

**Also writes `<NODE_IP> harbor` to /etc/hosts** — this is what makes the `harbor:30002` hostname in `certs.d/` resolvable from each node. Under IPv6-only kube-proxy, NodePort listeners on `[::]:30002` are NOT reachable via `::1` or `localhost`; only the node's own primary IP is routable. `NODE_IP` is supplied via the Downward API (`status.hostIP`). The DaemonSet rewrites /etc/hosts via a tmpfile + `cat > /etc/hosts` (preserves the kubelet bind-mount inode that `sed -i` and rename(2) editors cannot swap). The /etc/hosts write and the `certs.d/` config are equally load-bearing — modifying one without the other will break image pulls.

Runs on ALL nodes (no nodeSelector, tolerates everything).

### Node Performance Tuning

**File**: `base/kubernetes/node-performance-tuning.yaml`

Runs a privileged init container (`tune-node`) that configures:
- CPU governor set to `performance`
- Kubelet static CPU manager policy verification
- Topology manager policy verification
- NVIDIA GPU persistence mode (GPU nodes only)

Runs on nodes with `workload-type` in `[github-runner, buildkit]`.

### algif_aead Module Blacklist (TEMPORARY — CVE-2026-31431)

**File**: `base/kubernetes/algif-mitigation.yaml`

Writes `/etc/modprobe.d/disable-algif.conf` (`install algif_aead /bin/false`) and defensively `modprobe -r algif_aead` on every node. Mitigates CVE-2026-31431 ("Copy Fail" — Linux kernel `algif_aead` LPE that crosses container boundaries via the shared page cache). Privileged init container `nsenter`'s into PID 1's namespaces to operate on the host kernel.

Runs on ALL nodes (no nodeSelector, tolerates everything). Idempotent via `/var/lib/algif-mitigation/.configured` marker.

**TODO — REMOVE this DaemonSet** once all nodes are running an AL2023 AMI with kernel 6.12.85+. Watch https://explore.alas.aws.amazon.com/CVE-2026-31431.html and the AMI pinnings tagged with the same TODO marker (`clusters.yaml`, `modules/nodepools/scripts/python/generate_nodepools.py`, `modules/buildkit/scripts/python/generate_buildkit.py`, `modules/pypi-cache/kubernetes/ec2nodeclass.yaml.tpl`).

### DirtyFrag Mitigation (TEMPORARY — CVE-2026-43284 + CVE-2026-43500)

**File**: `base/kubernetes/dirtyfrag-mitigation.yaml`

Sibling DaemonSet to `algif-mitigation`, same shape (privileged `nsenter` into PID 1's namespaces, modprobe.d blacklist, marker file at `/var/lib/dirtyfrag-mitigation/.configured`, runs on EVERY node). Writes `/etc/modprobe.d/disable-dirtyfrag.conf` blacklisting three modules — `esp4`, `esp6`, `rxrpc` — then defensively `modprobe -r`'s any that have already loaded, then drops the page cache (`echo 3 > /proc/sys/vm/drop_caches`) to evict DirtyFrag-poisoned pages of read-only files. Mitigates two related Linux kernel write-LPEs in the IPv4/IPv6 datagram zero-copy path (xfrm-ESP and RxRPC variants) — both cross container boundaries via the shared page cache.

**TODO — REMOVE this DaemonSet** once all nodes are running an AL2023 AMI with kernel 6.1.170+ or 6.12.83+ (separate kernel-version gate from algif-mitigation's 6.12.85+). Watch AWS Security Bulletin 2026-027-AWS, ALAS2023-2026-1694, and ALAS2023-2026-1695.

## Taint Summary

| Taint Key | Type | Effect | Scope | Removed By |
|-----------|------|--------|-------|------------|
| `git-cache-not-ready=true` | Startup taint | `NoSchedule` | All Karpenter NodePools | git-cache-warmer DaemonSet |
| `instance-type={type}` | Permanent | `NoSchedule` | ARC runner NodePools | Never (scheduling constraint) |
| `node-fleet={fleet}` | Permanent | `NoSchedule` | ARC runner NodePools | Never (fleet-based scheduling) |
| `workload/buildkit-{arch}=true` | Permanent | `NoSchedule` | BuildKit NodePools | Never (scheduling constraint) |
| `nvidia.com/gpu=true` | Permanent | `NoSchedule` | GPU NodePools only — applied by both the standard `generate_nodepools.py` (g4dn, g5, g6, p4d) and the specialized H100 (`modules/nodepools-h100`) and B200 (`modules/nodepools-b200`) generators. The `topology_manager_policy: single-numa-node` (scope `pod`) override is a per-def opt-in (set in the fleet YAML) and is used by p4d in the standard generator AND by H100/B200 in the specialized generators; other GPU pools inherit the runner default (`best-effort`/`container`). | Never (scheduling constraint) |
| `node-compactor.osdc.io/consolidating=true` | Runtime (dynamic) | `NoSchedule` | Applied by node-compactor | node-compactor controller (protected by `min_node_age`: 900s) |
| `CriticalAddonsOnly=true` | Permanent | `NoSchedule` | Base infrastructure nodes (EKS-managed) | Never |

## Toleration Pattern

All DaemonSets targeting runner and buildkit nodes use a consistent toleration set covering `instance-type`, `node-fleet` (runner NodePools) and `workload/buildkit-*` (BuildKit NodePools) taints:

```yaml
tolerations:
  - key: instance-type
    operator: Exists
    effect: NoSchedule
  - key: node-fleet
    operator: Exists
    effect: NoSchedule
  - key: workload/buildkit-arm64
    operator: Exists
    effect: NoSchedule
  - key: workload/buildkit-amd64
    operator: Exists
    effect: NoSchedule
  - key: nvidia.com/gpu
    operator: Exists
    effect: NoSchedule
  - key: git-cache-not-ready
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

**Exception**: The `nodelocaldns` DaemonSet uses an **explicit** toleration list (NOT `operator: Exists` blanket-tolerating each key) — it explicitly tolerates `CriticalAddonsOnly`, `nvidia.com/gpu`, `node-fleet`, `instance-type`, and `git-cache-not-ready`. This is sufficient to cover all standard runner/buildkit/GPU NodePool taints. If a new permanent taint is added to any NodePool, NLD's toleration list must be updated explicitly.

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
| `base/kubernetes/algif-mitigation.yaml` | TEMPORARY: blacklists algif_aead module to mitigate CVE-2026-31431 (remove when AL2023 AMI has kernel 6.12.85+) |
| `base/kubernetes/dirtyfrag-mitigation.yaml` | TEMPORARY: blacklists esp4/esp6/rxrpc + drops page cache to mitigate CVE-2026-43284 + CVE-2026-43500 (remove when AL2023 AMI has kernel 6.1.170+ or 6.12.83+) |
| `modules/arc-runners/scripts/python/validate_runner_qos.py` | Deploy-time validation (checks hooks init container exists) |
