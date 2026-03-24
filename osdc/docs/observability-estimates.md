# Observability Cost Estimates

Per-unit cost estimates for metrics cardinality and log volume. For architecture, pipeline descriptions, deploy order, gotchas, and troubleshooting, see [observability.md](observability.md).

> **Maintenance note**: When adding, removing, or modifying any ServiceMonitor, PodMonitor, metricRelabelings, logging pipeline, sampling rate, or filtering rule, the estimates in this document **MUST be updated** to reflect the change. Stale estimates are misleading and can lead to unexpected Grafana Cloud costs.

## Metrics Cardinality Reference

Per-unit metrics cardinality estimates, broken down by scope. To calculate total cluster cardinality, multiply per-unit rates by actual replica/node/pod counts.

### Per Runner Pod

Runner pods are ephemeral — they run a single GitHub Actions job and terminate. Each runner pod produces metrics **only via cAdvisor** (built-in kubelet container metrics) and **indirectly via ARC listener metrics** (one listener per runner scale set, not per pod).

| Source | What's collected | ~Series |
|--------|-----------------|---------|
| **cAdvisor** (kubelet) | Container CPU (usage, throttling), memory (usage, working set, cache), filesystem (reads/writes, usage), network I/O (rx/tx bytes/packets) | ~30-50 per pod |
| **ARC listener** (per scale set, not per pod) | `gha_assigned_jobs`, `gha_job_execution_duration_seconds_{sum,count}`, `gha_job_startup_duration_seconds_{sum,count}` | ~5-10 per scale set |

**What's NOT collected per runner pod**: No application-level metrics from inside the runner. No per-job Prometheus endpoint. Runner pods do not expose a `/metrics` port. All runner-level telemetry is limited to what kubelet/cAdvisor observes externally.

### Per Karpenter Node

Every node managed by Karpenter runs several DaemonSets that produce metrics. The total varies significantly between CPU-only and GPU nodes.

| Source | Deployment | What's collected | ~Series per node |
|--------|-----------|-----------------|-----------------|
| **node-exporter** | DaemonSet (all nodes) | CPU (per-core), memory, disk I/O, filesystem, network, load averages, OS info | ~200-400 |
| **cAdvisor** | Built-in (kubelet) | Container metrics for ALL pods on the node (system pods + runner pods) | ~30-50 per container |
| **git-cache-warmer** | DaemonSet (all nodes) | Clone/fetch durations, cache sizes, rsync stats | ~10-20 |
| **DCGM exporter** | DaemonSet (GPU nodes only) | 26 curated GPU metrics per GPU: utilization, memory, temp, power, ECC, XID errors, NVLink, PCIe, clocks, throttling | 26 per GPU |

**Per-node totals** (varies by node type):

| Node type | node-exporter | cAdvisor | git-cache-warmer | DCGM | Total per node |
|-----------|:---:|:---:|:---:|:---:|:---:|
| CPU runner node (~10 containers) | ~300 | ~400 | ~15 | 0 | ~715 |
| GPU node (8x GPUs, ~10 containers) | ~300 | ~400 | ~15 | ~208 | ~923 |
| Base infra node (~30 containers) | ~300 | ~1200 | ~15 | 0 | ~1515 |

### Cluster-Wide (Fixed per Source)

These metrics come from centralized Deployments or StatefulSets. Series counts below are **per replica** unless noted. Total cluster overhead = sum of (per-replica rate × replica count) for each source.

| Source | What's collected | ~Series per replica |
|--------|-----------------|---------------------|
| **K8s API server** (EKS-managed) | Request rate/latency (sum/count), inflight requests, admission latency, storage/etcd latency, error rates. go_*/process_*/workqueue_* and all histogram buckets dropped at monitor level | ~200-400 total (EKS-managed, not scalable) |
| **CoreDNS** (EKS-managed) | Request/response counts, latency (sum/count), cache hits/misses/entries, forward request stats. go_*/process_* and histogram buckets dropped at monitor level | ~30-50 per replica |
| **kube-state-metrics** | K8s object state: pod/deployment/node/daemonset/statefulset/PV/HPA/replicaset/job/namespace status. Heavily filtered via allowlist + cost-control drops | ~200-500 total (single replica) |
| **Karpenter controller** | NodeClaim/node lifecycle, provisioning latency, disruption counts, nodepool capacity, interruption handling | ~25-50 per replica |
| **ARC controller** | Controller reconciliation, runner scale set status | ~10-25 per replica |
| **Harbor exporter** | Registry health, project/artifact/repository counts, storage quota, GC stats | ~50-100 per replica |
| **BuildKit daemon** | Build cache size, solver duration, active workers | ~6-12 per replica |
| **BuildKit HAProxy** | Connection counts, bytes in/out, session rates, health checks, queue depth | ~15-30 per replica |
| **git-cache-central** | Clone/fetch counts, cache hit rates, rsync performance | ~4-10 per replica |
| **node-compactor** | Compaction cycle counts, nodes tainted/untainted, utilization, burst events | ~10-20 total (single replica) |

### Scaling Formula (Metrics)

```
Total series ≈ C_fixed
             + N_runner_pods × 40
             + N_cpu_nodes × 715
             + N_gpu_nodes × 715 + 26 × N_total_GPUs
             + N_base_nodes × 1515
             + N_scale_sets × 8
```

Where:
- `C_fixed` = sum of all cluster-wide sources × their replica counts
- `N_total_GPUs` = total GPUs across all GPU nodes (handles heterogeneous fleets where GPU count varies by instance type)

### What's NOT Collected (Metrics)

| Source | Status | Notes |
|--------|--------|-------|
| etcd metrics | Not collected | EKS manages etcd; not exposed |
| CloudWatch / VPC / EBS metrics | Not collected | No CloudWatch exporter. Control plane logs go to CloudWatch (see Terraform) |
| kube-proxy metrics | Not scraped | Low signal-to-noise for CI workloads. Logs collected via logging pipeline |
| kubelet metrics (non-cAdvisor) | Partial | Prometheus Operator may auto-create kubelet scrape targets |
| kube-controller-manager metrics | Not collected | EKS does not expose controller-manager endpoints |
| kube-scheduler metrics | Not collected | EKS does not expose scheduler endpoints |

## Log Volume Estimation Reference

Per-unit log volume estimates sent to Grafana Cloud Loki, broken down by scope. All estimates account for pipeline filtering: DEBUG/TRACE drops, >16KB line drops, health-check drops, module-level sampling, Harbor rate limiting, and per-pod rate limits.

To calculate total cluster volume, multiply per-unit rates by actual replica/node/pod counts using the scaling formula at the end of this section.

### Per Runner Job Pod

Runner pods are ephemeral (minutes to hours). Each job creates a runner pod (ARC orchestrator) and a job pod (the CI workflow container). Both are in the `arc-runners` namespace, where **non-error logs are sampled at 10%** (errors/warns always kept).

| Component | Raw rate | After pipeline | ~Per job lifecycle (30 min) |
|-----------|----------|:-:|:-:|
| **Runner pod** (ARC orchestrator) | ~10-50 lines/min | ~2-10 lines/min | 50-300 lines (~0.01-0.06 MB) |
| **Job pod** (CI workflow) | ~100-10,000 lines/min | ~15-1,050 lines/min | 450-31,500 lines (~0.09-6.3 MB) |
| **Total per job** | | | **~0.1-6 MB** |

Pipeline stages applied (in order):
1. Pods in `Succeeded`/`Failed` phase excluded at discovery
2. DEBUG/TRACE lines dropped globally
3. Lines >16KB dropped
4. Health-check probes (`kube-probe/`) dropped
5. **Non-error logs sampled at 10%** (module pipeline — the dominant cost control)
6. Rate limit: 1000 lines/s per pod (rarely hit after sampling)

### Per Karpenter Node (DaemonSet Overhead)

Every Karpenter-managed node runs DaemonSets that produce logs. This is the per-node overhead **excluding** runner or BuildKit pods on the node.

| Source | Type | ~Lines/min per node | Notes |
|--------|------|:---:|-------|
| **kubelet** (journal) | System | 30-100 | Pod lifecycle, volume mounts, probes |
| **containerd** (journal) | System | 20-80 | Container start/stop, image pulls |
| **kernel** (journal) | System | 1-10 | OOM kills, hardware errors |
| **git-cache-warmer** | Pod log | 2-10 | rsync from central every 300s |
| **node-performance-tuning** | Pod log | ~0 | Init container only, main sleeps |
| **runner-hooks-warmer** | Pod log | ~0 | Downloads once, main sleeps |
| **registry-mirror-config** | Pod log | ~0 | Init container, main sleeps |
| **GPU: nvidia-fabricmanager** (journal) | System | 1-5 | GPU nodes only |
| **GPU: nvidia-persistenced** (journal) | System | 0-2 | GPU nodes only |

**Per-node totals**:

| Node type | Total overhead lines/min | ~GB/day per node |
|-----------|:---:|:---:|
| CPU runner node | ~55-200 | ~0.02-0.07 |
| GPU runner node | ~57-207 | ~0.02-0.07 |

**Not collected from nodes**: node-exporter (metrics only), alloy-logging (in `logging` namespace — self-excluded), dcgm-exporter (metrics only).

### Base Infrastructure Services (Per Replica)

These run on tainted `CriticalAddonsOnly` base nodes. Rates are **per replica** — multiply by actual replica count for each service.

| Source | ~Lines/min per replica | Notes |
|--------|:---:|-------|
| **Harbor nginx** | 15-170 | Access logs; health-checks dropped; non-error capped at 100 lines/s burst 500 |
| **Harbor core** | 5-25 | API request logs (JSON, level extracted) |
| **Harbor registry** | 5-50 | Layer push/pull logs |
| **Harbor jobservice** | 2-10 | Job execution logs |
| **Harbor portal** | 1-5 | Frontend serving logs |
| **Harbor db** | 1-5 | PostgreSQL logs |
| **Harbor redis** | 1-5 | Cache operation logs |
| **Harbor exporter** | 1-5 | Metrics exporter logs |
| **git-cache-central** | 2-10 | git fetch every 300s per replica |
| **node-compactor** | 10-30 | Runs every 20s, logs taint/untaint decisions (single replica) |
| **Karpenter controller** | 5-25 | Node provisioning/deprovisioning events |
| **ARC controller** | 5-25 | Runner scale set reconciliation |
| **ARC listener** | 1-5 | GitHub API polling (1 listener per runner scale set definition) |
| **CoreDNS** | 10-50 | DNS query logs |
| **kube-proxy** | 1-5 | Connection tracking (DaemonSet — 1 per base node) |
| **aws-node (VPC-CNI)** | 1-5 | Network allocation, JSON, level extracted (DaemonSet — 1 per base node) |
| **kube-state-metrics** | 2-10 | Object state change logs (single replica) |
| **Prometheus Operator** | 2-10 | CRD reconciliation (single replica) |
| **BuildKit HAProxy** | 5-20 | TCP connection access logs |
| **Alloy (monitoring)** | 2-10 | Scrape lifecycle logs |

Base nodes also produce journal logs (kubelet + containerd + kernel) at ~55-200 lines/min per node (same rates as the Karpenter node overhead table above).

Harbor nginx is the largest single source on base nodes. Without the 100 lines/s rate limit on non-error logs, nginx access logs alone could dominate total base infra volume during peak image pull activity.

### Per BuildKit Pod

BuildKit pods generate high log volume during active builds. **Non-error logs are sampled at 50%** (errors/warns always kept). Each pod runs `max-parallelism=1`.

| Source | Idle lines/min | Building lines/min | Notes |
|--------|:---:|:---:|-------|
| **buildkitd** (per pod) | 5-20 | 200-4,000 | After 50% sampling; every build step logged |

BuildKit nodes also produce DaemonSet + journal overhead at the same per-node rates as runner nodes (~55-200 lines/min per node).

### Kubernetes Events

Single `alloy-events` Deployment watches the K8s Events API across all namespaces except `logging`. Event volume scales with cluster activity, not replica count.

| Condition | ~Events/min | Notes |
|-----------|:---:|-------|
| Steady state (few pod changes) | 10-50 | Baseline cluster activity |
| Active scaling (runner pods churning) | 100-500 | Proportional to pod churn rate |
| Spike (mass scheduling event) | 500-2,000 | Transient |

Events are JSON (~700 bytes average). Fields extracted: `type` and `kind` to indexed labels; `reason` and `sourcecomponent` to structured metadata.

### Scaling Formula (Logs)

```
Total GB/day ≈ C_base_services
             + N_base_nodes × 0.02-0.07 GB      (journal overhead)
             + N_runner_nodes × 0.02-0.07 GB     (DaemonSet + journal overhead)
             + N_buildkit_nodes × 0.02-0.07 GB   (DaemonSet + journal overhead)
             + N_concurrent_jobs × job_MB × jobs_per_day / 1000
             + N_buildkit_pods × buildkit_GB_per_pod_day
             + events_GB
```

Where:
- `C_base_services` = sum of per-replica service rates × replica counts × 60 × 24 × ~200 bytes/line ÷ 1e9
- `job_MB` = 0.1-6 MB per job (highly variable by workflow)
- `buildkit_GB_per_pod_day` = depends on build concurrency (idle: ~0.01 GB, active: ~0.5 GB)
- `events_GB` = depends on cluster activity level (see events table above)

Runner pod logs are expected to be the **dominant log source** despite 90% sampling of non-error lines. The three most impactful cost controls, in order:

1. **ARC runner 10% sampling** — reduces runner logs by ~90%
2. **Harbor rate limiting** — caps non-error nginx access logs at 100 lines/s
3. **BuildKit 50% sampling** — reduces build logs by ~50%

Global filters (DEBUG/TRACE drop, >16KB drop, health-check drop) prevent baseline bloat but are harder to quantify individually.

### What's NOT Sent to Loki

| Source | Why not collected |
|--------|-------------------|
| Alloy pods (`logging` namespace) | Self-excluded at discovery level (feedback loop prevention) |
| Completed/failed pods | Dropped at discovery level |
| DEBUG/TRACE log lines | Globally dropped in base pipeline |
| Lines >16KB | Globally dropped (binary spam protection) |
| Health-check probe lines (`kube-probe/`) | Globally dropped |
| kube-apiserver logs | EKS-managed; not exposed as pod or journal unit |
| kube-scheduler logs | EKS-managed; not exposed |
| kube-controller-manager logs | EKS-managed; not exposed |
| etcd logs | EKS-managed; not exposed |
| CloudWatch control plane logs | Separate AWS pipeline; not collected by Alloy |

---

## Estimated Metrics Load for arc-cbr-production

Peak active series estimate for the `arc-cbr-production` cluster, derived from the fleet simulation in [current_runner_load_distribution.md](current_runner_load_distribution.md) and the per-unit rates above. Represents the worst-case simultaneous peak where every runner type hits its individual peak concurrency at the same time.

**Date:** 2026-03-24. **Scope:** pytorch/pytorch self-hosted Linux runners only (same scope as the fleet simulation). Actual cardinality will be slightly higher due to unmapped runner types, B200 GPU runners (local `arc-runners-b200` module, not in fleet simulation), and other repos sharing the cluster.

### Input data

From the fleet simulation:

| Parameter | Value | Source |
|---|---|---|
| Runner pods at peak | 7,367 | Sum of per-type peak concurrency |
| CPU-only runner nodes | 680 | c7a.48xlarge (267) + c7i.metal-24xl (36) + m6i.32xlarge (27) + m7i.48xlarge (48) + m8g.16xlarge (29) + m8g.48xlarge (15) + r7a.48xlarge (138) + r7g.16xlarge (120) |
| GPU runner nodes | 1,433 | g4dn.12xlarge (157) + g4dn.8xlarge (89) + g4dn.metal (79) + g5.12xlarge (58) + g5.48xlarge (40) + g5.8xlarge (596) + g6.12xlarge (26) + g6.8xlarge (388) |
| Total GPUs | 2,989 | Across all GPU node types |
| Base infra nodes | 5 | `clusters.yaml` → `base_node_count` (m7i.4xlarge, fixed) |
| Runner scale sets | 40 | 36 upstream (`modules/arc-runners/defs/`) + 4 B200 (`modules/arc-runners-b200/defs/`) |

From `clusters.yaml` defaults for `arc-cbr-production`:

| Service | Replicas |
|---|---|
| Karpenter | 2 |
| ARC controller | 2 |
| Harbor nginx | 3 |
| Harbor core | 2 |
| Harbor registry | 2 |
| Harbor exporter / jobservice / portal / db / redis | 1 each |
| git-cache-central | 5 |
| node-compactor | 1 |
| Monitoring Alloy | 2 | `clusters.yaml` → `monitoring.alloy_replicas` default |
| BuildKit daemon | 8 (4 per arch) |
| BuildKit HAProxy | 1 |
| CoreDNS | 2 (EKS default) |
| kube-state-metrics | 1 |
| Prometheus Operator | 1 |

### Cluster-wide fixed overhead (C_fixed)

Using midpoint of per-replica ranges from the [cluster-wide reference table](#cluster-wide-fixed-per-source):

| Source | ~Series/replica (mid) | Replicas | Subtotal |
|---|---|---|---|
| K8s API server | ~300 | 1 | ~300 |
| CoreDNS | ~40 | 2 | ~80 |
| kube-state-metrics | ~350 | 1 | ~350 |
| Karpenter | ~38 | 2 | ~76 |
| ARC controller | ~18 | 2 | ~36 |
| Harbor exporter | ~75 | 1 | ~75 |
| BuildKit daemon | ~9 | 8 | ~72 |
| BuildKit HAProxy | ~23 | 1 | ~23 |
| git-cache-central | ~7 | 5 | ~35 |
| node-compactor | ~15 | 1 | ~15 |
| **Total C_fixed** | | | **~1,062** |

### Peak cardinality calculation

Applying the [scaling formula](#scaling-formula-metrics):

```
Total series ≈ C_fixed
             + N_runner_pods × 40
             + N_cpu_nodes × 715
             + N_gpu_nodes × 715 + 26 × N_total_GPUs
             + N_base_nodes × 1515
             + N_scale_sets × 8

           ≈ 1,062
             + 7,367 × 40               =    294,680
             + 680 × 715                =    486,200
             + 1,433 × 715 + 26 × 2,989 =  1,102,309
             + 5 × 1,515               =      7,575
             + 40 × 8                   =        320

           ≈ 1,892,146
```

### Summary

| Component | ~Active Series | % of Total |
|---|---|---|
| GPU runner nodes (node-exporter + cAdvisor + DCGM) | ~1,102,000 | 58.2% |
| CPU runner nodes (node-exporter + cAdvisor) | ~486,000 | 25.7% |
| Runner pods (cAdvisor) | ~295,000 | 15.6% |
| Base infra nodes | ~7,600 | 0.4% |
| Cluster-wide fixed | ~1,100 | 0.1% |
| Scale sets (ARC listener) | ~300 | <0.1% |
| **Total at peak** | **~1,892,000** | |

**~1.9M active series** at simultaneous peak. GPU runner nodes dominate (58%) due to DCGM per-GPU metrics stacking on top of the standard per-node series.

### Scaling at sub-peak load

Active series scale roughly linearly with fleet size. Using the fleet estimates from the [node churn section](current_runner_load_distribution.md#estimated-node-churn):

| Load level | Est. Nodes | Est. Active Series |
|---|---|---|
| Weekday peak (10–11am ET) | ~2,113 | ~1,892,000 |
| Weekday trough (2–3am ET) | ~597 | ~535,000 |
| Weekend sustained | ~249 | ~223,000 |

---

## Estimated Log Volume for arc-cbr-production

Estimated daily log volume (GB/day) sent to Grafana Cloud Loki, derived from the fleet simulation in [current_runner_load_distribution.md](current_runner_load_distribution.md) and the per-unit rates above.

**Date:** 2026-03-24. **Scope:** pytorch/pytorch self-hosted Linux runners only (same scope as the fleet simulation). Actual volume will be slightly higher due to unmapped runner types, B200 GPU runners (local `arc-runners-b200` module, not in fleet simulation), and other repos sharing the cluster.

### Input data

From the load distribution data:

| Parameter | Value | Source |
|---|---|---|
| Total jobs (30 days) | ~1,599,000 | Sum of all self-hosted Linux runner job counts |
| Average jobs/day | ~53,300 | 1,599,000 / 30 |
| Peak fleet | 2,113 nodes | Fleet simulation |
| Weekday avg fleet | ~1,355 nodes | Derived from [node churn estimates](current_runner_load_distribution.md#fleet-size-by-time-of-day): weighted average of peak (2,113), trough (597), and transition periods |
| Weekend avg fleet | ~249 nodes | [Node churn estimates](current_runner_load_distribution.md#fleet-size-by-time-of-day) |
| BuildKit pods | 8 (4 per arch) | `clusters.yaml` → `buildkit.replicas_per_arch: 4` |
| BuildKit nodes | 4 (2 per arch) | 2 pods per node |
| Base nodes | 5 | `clusters.yaml` → `base_node_count` |

### Component breakdown (weekday average)

#### Node DaemonSet + journal overhead

Every runner node produces 0.02–0.07 GB/day (midpoint ~0.045) in DaemonSet and journal logs. This scales linearly with fleet size.

| Load level | Avg runner nodes | GB/day (low) | GB/day (mid) | GB/day (high) |
|---|---|---|---|---|
| Weekday average | ~1,355 | 27.1 | 61.0 | 94.9 |
| Weekend average | ~249 | 5.0 | 11.2 | 17.4 |

BuildKit nodes (4) and base nodes (5) add a constant ~0.4 GB/day (mid) regardless of runner load.

#### Base infrastructure services

Fixed overhead from centralized services. Uses service rates from the [base infrastructure reference](#base-infrastructure-services-per-replica), multiplied by replica counts for `arc-cbr-production`, converted at ~200 bytes/line.

| Service | Lines/min (mid × replicas) | GB/day |
|---|---|---|
| Harbor (all components) | 3×93 + 2×15 + 2×28 + 1×6 + 1×3 + 1×3 + 1×3 + 1×3 = ~380 | ~0.11 |
| ARC listeners (40 scale sets) | 40 × 3 = ~120 | ~0.03 |
| ARC controller + Karpenter + CoreDNS | 2×15 + 2×15 + 2×30 = ~120 | ~0.03 |
| git-cache-central (5 replicas) | 5 × 6 = ~30 | ~0.01 |
| Other (node-compactor, kube-proxy, aws-node, KSM, Prom Operator, Alloy, HAProxy) | ~90 | ~0.03 |
| **Total base services** | **~740** | **~0.21** |

#### Runner job logs

This is the dominant variable. Each runner job produces 0.1–6 MB of logs after 10% non-error sampling (see [per runner job pod reference](#per-runner-job-pod)). The wide range reflects the mix of small lint/test jobs (~0.1–0.5 MB) vs. large GPU test jobs (~2–6 MB).

At ~53,300 jobs/day average:

| Average per-job volume | Runner logs GB/day |
|---|---|
| 0.5 MB (lint/test dominated) | ~26.7 |
| 1.0 MB | ~53.3 |
| 2.0 MB | ~106.6 |
| 3.0 MB (GPU-test dominated) | ~159.9 |

**Unknown:** actual per-job log volume distribution. The job mix is heavily weighted toward small CPU runners (linux.2xlarge: 491K jobs, linux.c7i.2xlarge: 351K) which likely produce lower log volume. An empirical measurement from Loki (average bytes/job across a sample period) would narrow this range significantly.

#### BuildKit logs

8 pods at `max-parallelism=1`. Volume depends on build activity (not in the load data).

| Build activity | GB/day |
|---|---|
| Mostly idle | ~0.08 (8 × 0.01) |
| Fully active | ~4.0 (8 × 0.5) |

#### Kubernetes events

Pod churn drives event volume. At ~53,300 jobs/day (~37 job starts/min average), plus DaemonSet scheduling and node lifecycle events:

| Cluster activity | Est. events/min | GB/day |
|---|---|---|
| Average (weekday) | ~200 | ~0.20 |
| Peak (ramp-up/scale-down) | ~500 | ~0.50 |

### Total daily volume estimate (weekday average)

Using midpoint per-unit rates and ~1 MB/job as the runner job estimate:

| Component | GB/day | % of Total |
|---|---|---|
| Node DaemonSet + journal overhead | ~61 | 52.8% |
| Runner job logs (at 1 MB/job) | ~53 | 45.9% |
| Base infrastructure services | ~0.2 | 0.2% |
| BuildKit (mixed idle/active) | ~1 | 0.9% |
| Events | ~0.2 | 0.2% |
| **Total** | **~116** | |

### Sensitivity to per-job log volume

The two dominant cost drivers are node DaemonSet overhead and runner job logs. Node overhead is relatively fixed for a given fleet size. Runner job volume is the primary lever:

| Per-job volume | Runner GB/day | Node overhead GB/day | Total GB/day | Monthly GB |
|---|---|---|---|---|
| 0.5 MB | 26.7 | 61 | ~89 | ~2,670 |
| 1.0 MB | 53.3 | 61 | ~116 | ~3,480 |
| 2.0 MB | 106.6 | 61 | ~169 | ~5,070 |
| 3.0 MB | 159.9 | 61 | ~223 | ~6,690 |

Monthly totals assume weekday average; actual monthly volume will be ~15–20% lower due to reduced weekend load.

### Caveats

1. **Sum-of-peaks fleet size**: the fleet simulation uses the sum of each runner type's individual peak concurrency (7,367 runners). Since these peaks don't all occur simultaneously, the weekday average fleet (~1,355 nodes) used for node overhead is already a more realistic baseline than the peak (2,113).

2. **Per-job log volume is the largest unknown**: the 0.1–6 MB range spans 60×. The job mix is dominated by small CPU runners which likely produce <1 MB, but GPU test jobs (93K+ g5.4xlarge jobs) may produce 3–6 MB each. Querying actual Loki ingestion rate per namespace would resolve this.

3. **Node overhead may be over-estimated**: the 0.02–0.07 GB/day/node range includes journal logs (kubelet, containerd, kernel). The high end assumes verbose kubelet logging during active pod scheduling, which is transient. Sustained average is likely closer to the low end (0.02–0.03 GB/day/node).

4. **Logging module status**: as of this estimate date, the `logging` module is not in the `arc-cbr-production` modules list but is deployed as base infrastructure. Log shipping requires Grafana Cloud credentials — until the credentials secret exists in the cluster, no logs are sent. These estimates project what the cluster *will* send once logging is active.
