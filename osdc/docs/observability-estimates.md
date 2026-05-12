# Observability Cost Estimates

Per-unit cost estimates for metrics cardinality and log volume. For architecture, pipeline descriptions, deploy order, gotchas, and troubleshooting, see [observability.md](observability.md).

> **Stale estimates warning (2026-05-06)**: The numerical cardinality estimates in the "Estimated Metrics Load for arc-cbr-production" section below predate the comprehensive metric whitelisting work landed in PR #418 / #428 (commits b59f269, 18dcf3b). Per-source descriptions have been updated to reflect the current whitelists, but the aggregate totals and percentages have **not** been recomputed. They are likely overstated by 1–2 orders of magnitude for sources like node-exporter (now 2 series per node, was estimated at 200–400) and cAdvisor (now 2 series per container restricted to control-plane namespaces, was estimated at 30–50 cluster-wide). Re-validate against live Grafana Cloud Mimir before relying on these numbers (e.g. `count by (__name__) ({cluster="pytorch-arc-cbr-production"})`).

> **Maintenance note**: When adding, removing, or modifying any ServiceMonitor, PodMonitor, metricRelabelings, logging pipeline, sampling rate, or filtering rule, the estimates in this document **MUST be updated** to reflect the change. Stale estimates are misleading and can lead to unexpected Grafana Cloud costs.

## Metrics Cardinality Reference

Per-unit metrics cardinality estimates, broken down by scope. To calculate total cluster cardinality, multiply per-unit rates by actual replica/node/pod counts.

### Per Runner Pod

Runner pods are ephemeral — they run a single GitHub Actions job and terminate. Each runner pod produces metrics **only via cAdvisor** (built-in kubelet container metrics) and **indirectly via ARC listener metrics** (one listener per runner scale set, not per pod).

| Source | What's collected | ~Series |
|--------|-----------------|---------|
| **cAdvisor** (kubelet) | Whitelisted to `container_memory_working_set_bytes` and `container_memory_rss` only at the kubelet ServiceMonitor level. Further restricted by Alloy `cost_control` to control-plane namespaces only (`arc-systems\|karpenter\|harbor-system\|monitoring\|logging\|buildkit`). Runner pods contribute zero cAdvisor series. | 0 per runner pod (filtered out at Alloy) |
| **ARC listener** (per scale set, not per pod) | `gha_assigned_jobs`, `gha_completed_jobs_total`, `gha_started_jobs_total`, `gha_running_jobs`, `gha_job_execution_duration_seconds_{sum,count}`, `gha_job_startup_duration_seconds_{sum,count}`, plus the 15 `gha_capacity_*` metric families (proactive-capacity monitor) | ~85-90 per scale set |

**What's NOT collected per runner pod**: No application-level metrics from inside the runner. No per-job Prometheus endpoint. Runner pods do not expose a `/metrics` port. All runner-level telemetry is limited to what kubelet/cAdvisor observes externally.

### Per Karpenter Node

Every node managed by Karpenter runs several DaemonSets that produce metrics. The total varies significantly between CPU-only and GPU nodes.

| Source | Deployment | What's collected | ~Series per node |
|--------|-----------|-----------------|-----------------|
| **node-exporter** | DaemonSet (all nodes) | Whitelisted to memory only: `node_memory_MemAvailable_bytes` and `node_memory_MemTotal_bytes`. All other collectors (CPU, disk, fs, network, load, OS info) are dropped at ServiceMonitor level. | ~2 per node |
| **kubelet** | Built-in (kubelet ServiceMonitor) | Whitelisted to `kubelet_running_pods`, `kubelet_running_containers`, `kubelet_node_name`. `/metrics/probes` disabled. | small fixed set per node |
| **cAdvisor** | Built-in (kubelet) | Whitelisted to `container_memory_working_set_bytes` and `container_memory_rss`, then restricted by Alloy `cost_control` to control-plane namespaces only (`arc-systems\|karpenter\|harbor-system\|monitoring\|logging\|buildkit`). Runner nodes contribute zero cAdvisor series. | ~2 per control-plane container; 0 per runner pod |
| **git-cache-warmer** | DaemonSet (all nodes) | Clone/fetch durations, cache sizes, rsync stats | ~10-20 |
| **nodelocaldns** | DaemonSet (all nodes) | Two scrape ports: `:9253` exposes CoreDNS plugin metrics per zone (4 zones — `cluster.local`, `in-addr.arpa`, `ip6.arpa`, `.`) — request counts by type/proto/family, response codes, cache hits/misses/entries, forward stats, plus `_sum`/`_count` for duration histograms. `:9353` exposes the binary's `coredns_nodecache_setup_errors_total` (single series). Drops `go_*`, `process_*`, and `coredns_*_request_duration_seconds_bucket` at PodMonitor level (matches `coredns.yaml` convention). | ~40-60 per node (~10-15 per zone × 4 zones, plus 1 setup-errors series) |
| **DCGM exporter** | DaemonSet (GPU nodes only) | 25 curated GPU metrics per GPU (Tier 1: 22, Tier 2: 3): utilization, memory, temp, power, ECC, XID errors, NVLink, PCIe, clocks, throttling | 25 per GPU |

**Per-node totals** (varies by node type — see the stale-estimates warning at the top of this doc; the numbers below are pre-whitelist and overstate cardinality by 1–2 orders of magnitude):

| Node type | node-exporter | cAdvisor | git-cache-warmer | nodelocaldns | DCGM | Total per node |
|-----------|:---:|:---:|:---:|:---:|:---:|:---:|
| CPU runner node (~10 containers) | ~300 | ~400 | ~15 | ~50 | 0 | ~765 |
| GPU node (8x GPUs, ~10 containers) | ~300 | ~400 | ~15 | ~50 | ~208 | ~973 |
| Base infra node (~30 containers) | ~300 | ~1200 | ~15 | ~50 | 0 | ~1565 |

### Cluster-Wide (Fixed per Source)

These metrics come from centralized Deployments or StatefulSets. Series counts below are **per replica** unless noted. Total cluster overhead = sum of (per-replica rate × replica count) for each source.

| Source | What's collected | ~Series per replica |
|--------|-----------------|---------------------|
| **K8s API server** (EKS-managed) | Whitelisted to `apiserver_request_total` and `apiserver_request_terminations_total` only. | small fixed set (whitelist of 2 metric families) |
| **CoreDNS** (EKS-managed) | Request/response counts, latency (sum/count), cache hits/misses/entries, forward request stats. go_*/process_* and histogram buckets dropped at monitor level | ~30-50 per replica |
| **kube-state-metrics** | K8s object state: pod/deployment/node/daemonset/statefulset/PV/HPA/job/namespace status. Allowlist scope: `daemonset|deployment|pod|namespace|node|statefulset|persistentvolume|horizontalpodautoscaler|job` (replicaset NOT included). Heavily filtered via allowlist + cost-control drops. | ~200-500 total (single replica) |
| **Karpenter controller** | Whitelisted to 7 metric families: `karpenter_nodeclaims_created_total`, `karpenter_nodes_created_total`, `karpenter_nodes_terminated_total`, `karpenter_nodepools_usage`, `karpenter_nodepools_limit`, `karpenter_nodes_allocatable`, `karpenter_interruption_received_messages_total`. No latency, histograms, or disruption counts. | small fixed set per replica |
| **ARC controller** | Whitelisted to `gha_controller_.*` and `controller_runtime_reconcile_errors_total`. | ~10-25 per replica |
| **Harbor exporter** | No positive whitelist — only `go_*\|process_*\|promhttp_*` are dropped at the ServiceMonitor level. Actual cardinality is the full Harbor metric set; estimate is approximate. | ~50-100 per replica (unverified upper bound) |
| **BuildKit daemon** | Drops `go_*`, `process_*`, `promhttp_*`, `target_info`, and all `_bucket` histograms; no positive whitelist. Cardinality could be higher than this estimate depending on remaining `_sum/_count` series. | ~6-12 per replica (unverified) |
| **BuildKit HAProxy** | Whitelisted to 4 metrics only: `haproxy_server_status`, `haproxy_server_current_sessions`, `haproxy_server_connection_errors_total`, `haproxy_backend_current_sessions`. No bytes in/out, rates, or queue depth. | small fixed set per replica |
| **git-cache-central** | Clone/fetch counts, cache hit rates, rsync performance | ~4-10 per replica |
| **node-compactor** | Compaction cycle counts, nodes tainted/untainted, utilization, burst events | ~10-20 total (single replica) |
| **Pushgateway** | Hosts metrics pushed by CronJobs (zombie-cleanup, etc.). `honorLabels: true`. Volume depends on which jobs push and how many label combinations they emit. | varies (push-based, job-dependent) |
| **pypi-cache nginx** | Whitelisted to `nginx_up`, `nginx_http_requests_total`, `nginx_connections_active` per nginx-exporter sidecar (one per pod — 4 deployments × N replicas per cluster). | small fixed set per pod |

### Scaling Formula (Metrics)

> **NOTE:** the per-node and per-pod multipliers below are stale (see warning at top of doc). They predate the metric whitelisting work — node-exporter is now ~2 series per node (not ~300) and runner-pod cAdvisor is now 0 series (not 40). The DCGM term should use 25, not 26 (the curated metrics CSV defines exactly 25 metrics). The nodelocaldns term (~50 per node) is post-whitelist and stable. Use the formula structure but expect totals to be 1–2 orders of magnitude lower in practice.

```
Total series ≈ C_fixed
             + N_runner_pods × 40
             + N_cpu_nodes × 765      (was 715; +50 nodelocaldns)
             + N_gpu_nodes × 765 + 25 × N_total_GPUs
             + N_base_nodes × 1565    (was 1515; +50 nodelocaldns)
             + N_scale_sets × 85
```

Where:
- `C_fixed` = sum of all cluster-wide sources × their replica counts
- `N_total_GPUs` = total GPUs across all GPU nodes (handles heterogeneous fleets where GPU count varies by instance type)
- The `N_scale_sets × 85` term reflects the listener's full per-scale-set series count: ~5-10 baseline (`gha_assigned_jobs`, `gha_completed_jobs_total`, `gha_started_jobs_total`, `gha_running_jobs`, job-duration sum/count) plus ~77 from the 15 `gha_capacity_*` families (the two histograms — `gha_capacity_reconcile_duration_seconds` and `gha_capacity_hud_request_duration_seconds` — contribute ~22 each via 9 buckets + sum + count across two label values; `gha_capacity_placeholder_pods` contributes 10 via the `role`(2)×`phase`(5) cross-product; the remaining gauges/counters add the rest)

### What's NOT Collected (Metrics)

| Source | Status | Notes |
|--------|--------|-------|
| etcd metrics | Not collected | EKS manages etcd; not exposed |
| CloudWatch / VPC / EBS metrics | Not collected | No CloudWatch exporter. Control plane logs go to CloudWatch (see Terraform) |
| kube-proxy metrics | Not scraped | Low signal-to-noise for CI workloads. Logs collected via logging pipeline |
| kubelet metrics (non-cAdvisor) | Partial | Whitelisted to `kubelet_running_pods`, `kubelet_running_containers`, `kubelet_node_name`. `/metrics/probes` disabled. |
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
| **cache-enforcer** | Pod log | 1-5 | DaemonSet on `arc-cbr-production` (`modules/cache-enforcer/kubernetes/daemonset.yaml`) |
| **image-cache-janitor** | Pod log | 1-5 | DaemonSet in base — runs on every node (`base/kubernetes/image-cache-janitor/daemonset.yaml`) |
| **nodelocaldns** | Pod log | <1 | DaemonSet in base — runs on every node (`base/kubernetes/nodelocaldns/`). CoreDNS plugin reports + "Setup OK" messages only; near-silent at steady state |

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

**Date:** 2026-03-24 (input data); per-source descriptions and module/scale-set composition refreshed 2026-05-06. The aggregate totals below have **not** been recomputed against the current whitelists — see the stale-estimates warning at the top of this doc. **Scope:** pytorch/pytorch self-hosted Linux runners only (same scope as the fleet simulation). Actual cardinality will also be higher due to unmapped runner types, B200/H100 GPU runners (local `arc-runners-b200`/`arc-runners-h100` modules, not in fleet simulation), and other repos sharing the cluster.

### Input data

From the fleet simulation:

| Parameter | Value | Source |
|---|---|---|
| Runner pods at peak | 7,367 | Sum of per-type peak concurrency |
| CPU-only runner nodes | 680 | c7a.48xlarge (267) + c7i.metal-24xl (36) + m6i.32xlarge (27) + m7i.48xlarge (48) + m8g.16xlarge (29) + m8g.48xlarge (15) + r7a.48xlarge (138) + r7g.16xlarge (120) |
| GPU runner nodes | 1,433 | g4dn.12xlarge (157) + g4dn.8xlarge (89) + g4dn.metal (79) + g5.12xlarge (58) + g5.48xlarge (40) + g5.8xlarge (596) + g6.12xlarge (26) + g6.8xlarge (388). H100 (`p5.48xlarge`) and B200 nodes are not in the fleet simulation and are not counted here. |
| Total GPUs | 2,989 | Across all GPU node types in the fleet simulation (excludes H100/B200) |
| Base infra nodes | 5 | `clusters.yaml` → `base_node_count` (m7i.4xlarge, fixed) |
| Runner scale sets | 50 | 42 upstream (`modules/arc-runners/defs/`) + 4 B200 (`modules/arc-runners-b200/defs/`) + 4 H100 (`modules/arc-runners-h100/defs/`) |

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
| Monitoring Alloy | 2 | Hard-coded in `modules/monitoring/helm/alloy-values.yaml` (`controller.replicas: 2`, `clustering.enabled: true`) — there is no `monitoring.alloy_replicas` config knob |
| BuildKit daemon | 8 (4 per arch — `buildkit.replicas_per_arch: 4` × 2 archs) |
| BuildKit HAProxy | 1 |
| CoreDNS | 2 (EKS default) |
| kube-state-metrics | 1 |
| Prometheus Operator | 1 |
| pypi-cache nginx | 20 (4 deployments per CUDA slug × 5 replicas — `pypi_cache.replicas: 5`) |

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

Applying the [scaling formula](#scaling-formula-metrics). **The numerical totals below are stale** — they predate the metric whitelisting work (PR #418, #428) that landed after this calculation, and they use the old scale-set count (40 → now 50) and the off-by-one DCGM constant (26 → actual 25). The structure is preserved for reference; re-validate against live Mimir before relying on these numbers.

```
Total series ≈ C_fixed
             + N_runner_pods × 40
             + N_cpu_nodes × 715
             + N_gpu_nodes × 715 + 25 × N_total_GPUs
             + N_base_nodes × 1515
             + N_scale_sets × 85

           ≈ 1,062
             + 7,367 × 40               =    294,680   (stale: per-pod cAdvisor now 0)
             + 680 × 715                =    486,200   (stale: per-node node-exporter now ~2)
             + 1,433 × 715 + 25 × 2,989 =  1,099,320   (stale: per-node node-exporter now ~2; H100 GPUs not counted)
             + 5 × 1,515               =      7,575   (stale: per-node multiplier overstated)
             + 50 × 85                  =      4,250   (was 40 × 85 = 3,400 before H100 module + 6 added scale sets)

           ≈ stale; do not rely on aggregate totals
```

### Summary

> **Stale table — do not rely on these numbers.** The "Runner pods (cAdvisor)" line is now 0 (cAdvisor restricted to control-plane namespaces). The GPU/CPU runner node lines are overstated by 1–2 orders of magnitude (node-exporter now ~2 series per node, not ~300). The "Scale sets" line at 50 × ~85 ≈ ~4,250 (was 40 × 85 in this table). Totals not recomputed — see warning at top of doc.

| Component | ~Active Series (stale) | % of Total (stale) |
|---|---|---|
| GPU runner nodes (node-exporter + cAdvisor + DCGM) | ~1,102,000 | 58.2% |
| CPU runner nodes (node-exporter + cAdvisor) | ~486,000 | 25.7% |
| Runner pods (cAdvisor) | ~295,000 | 15.5% |
| Base infra nodes | ~7,600 | 0.4% |
| Scale sets (ARC listener) | ~3,400 | 0.2% |
| Cluster-wide fixed | ~1,100 | 0.1% |
| **Total at peak** | **~1,895,000** | |

**~1.9M active series** at simultaneous peak. **WARNING:** this total is stale and overstated — see the warning at the top of this doc; the per-source whitelisting work landed after this calculation was made. GPU runner nodes dominate the (stale) breakdown due to DCGM per-GPU metrics. The ARC listener line grew from ~300 to ~4,250 series with the proactive-capacity metrics at the 50-scale-set count for `arc-cbr-production` (50 × ~85), but is still a small fraction of total. Re-validate against live Mimir before relying on these numbers.

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

**Date:** 2026-03-24 (input data); per-source descriptions and module/scale-set composition refreshed 2026-05-06. **Scope:** pytorch/pytorch self-hosted Linux runners only (same scope as the fleet simulation). Actual volume will also be higher due to unmapped runner types, B200/H100 GPU runners (local `arc-runners-b200`/`arc-runners-h100` modules, not in fleet simulation), and other repos sharing the cluster.

### Input data

From the load distribution data:

| Parameter | Value | Source |
|---|---|---|
| Total jobs (30 days) | ~1,599,000 | Sum of all self-hosted Linux runner job counts |
| Average jobs/day | ~53,300 | 1,599,000 / 30 |
| Peak fleet | 2,113 nodes | Fleet simulation |
| Weekday avg fleet | ~1,355 nodes | Derived from [node churn estimates](current_runner_load_distribution.md#fleet-size-by-time-of-day): weighted average of peak (2,113), trough (597), and transition periods |
| Weekend avg fleet | ~249 nodes | [Node churn estimates](current_runner_load_distribution.md#fleet-size-by-time-of-day) |
| BuildKit pods | 8 (4 per arch) | `clusters.yaml` → `buildkit.replicas_per_arch: 4` × 2 archs |
| BuildKit nodes | 4 (2 per arch) | `clusters.yaml` arc-cbr-production override → `buildkit.pods_per_node: 2` |
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
| ARC listeners (50 scale sets) | 50 × 3 = ~150 | ~0.04 |
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
