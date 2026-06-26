# Observability Cost Estimates

Per-unit cost estimates for metrics cardinality and log volume. For architecture, pipeline descriptions, deploy order, gotchas, and troubleshooting, see [observability.md](observability.md).

> **Stale estimates warning**: The numerical cardinality estimates in the "Estimated Metrics Load for arc-cbr-production" section below are stale for multiple reasons and should not be relied on without re-validation:
> - **Per-source whitelists landed after the original calculation.** node-exporter is now ~4 series per node (was estimated at 200–400) and cAdvisor is restricted to control-plane namespaces (was estimated at 30–50 cluster-wide).
> - **The kubelet ServiceMonitor is currently disabled** on IPv6-only EKS (`modules/monitoring/helm/values.yaml` `kubelet.enabled: false`). Both cAdvisor and `kubelet_*` series produce **zero** today. The per-source descriptions below describe the planned post-IPv6 state, not current behavior. See `observability.md` "Disabled scrapers (IPv6 migration)".
> - **`arc-cbr-production` sizing inputs in this doc are out of date.** Current `clusters.yaml` defaults give: base nodes 6 (`m7i.12xlarge`), CoreDNS 6, Karpenter 4, ARC controller 4, Harbor nginx/core/registry 6/4/4, BuildKit 10 (5/arch × 2), pypi-cache 40 (4 deployments × 10 replicas). The input-data table below has been refreshed; subtotals downstream may still reflect the older numbers.
> - **Scale-set count is 50**, not 40 (42 upstream + 4 B200 + 4 H100).
> - **`gha_capacity_*` family count is 16**, not 15.
>
> Re-validate against live Grafana Cloud Mimir before relying on aggregate numbers (e.g. `count by (__name__) ({cluster="pytorch-arc-cbr-production"})`).

> **Maintenance note**: When adding, removing, or modifying any ServiceMonitor, PodMonitor, metricRelabelings, logging pipeline, sampling rate, or filtering rule, the estimates in this document **MUST be updated** to reflect the change. Stale estimates are misleading and can lead to unexpected Grafana Cloud costs.

## Metrics Cardinality Reference

Per-unit metrics cardinality estimates, broken down by scope. To calculate total cluster cardinality, multiply per-unit rates by actual replica/node/pod counts.

### Per Runner Pod

Runner pods are ephemeral — they run a single GitHub Actions job and terminate. Each runner pod produces metrics **only via cAdvisor** (built-in kubelet container metrics) and **indirectly via ARC listener metrics** (one listener per runner scale set, not per pod).

| Source | What's collected | ~Series |
|--------|-----------------|---------|
| **cAdvisor** (kubelet) | **Currently 0 series — kubelet ServiceMonitor is disabled** (`modules/monitoring/helm/values.yaml` `kubelet.enabled: false`, IPv6 migration). Planned post-IPv6 state: whitelisted to `container_memory_working_set_bytes` and `container_memory_rss` only at the kubelet ServiceMonitor level, then further restricted by Alloy `cost_control` to control-plane namespaces only (`arc-systems\|karpenter\|harbor-system\|monitoring\|logging\|buildkit`). Runner pods will contribute zero cAdvisor series even after re-enablement. | 0 per runner pod (kubelet SM off) |
| **ARC listener** (per scale set, not per pod) | `gha_assigned_jobs`, `gha_completed_jobs_total`, `gha_started_jobs_total`, `gha_running_jobs`, `gha_job_execution_duration_seconds_{sum,count}`, `gha_job_startup_duration_seconds_{sum,count}`, plus the 16 `gha_capacity_*` metric families (proactive-capacity monitor) | ~85-90 per scale set |

**What's NOT collected per runner pod**: No application-level metrics from inside the runner. No per-job Prometheus endpoint. Runner pods do not expose a `/metrics` port. All runner-level telemetry is limited to what kubelet/cAdvisor observes externally.

### Per Karpenter Node

Every node managed by Karpenter runs several DaemonSets that produce metrics. The total varies significantly between CPU-only and GPU nodes.

| Source | Deployment | What's collected | ~Series per node |
|--------|-----------|-----------------|-----------------|
| **node-exporter** | DaemonSet (all nodes) | Whitelisted to 4 metrics: `node_memory_MemAvailable_bytes`, `node_memory_MemTotal_bytes`, `node_nf_conntrack_entries`, `node_nf_conntrack_entries_limit`. Conntrack metrics power `ipv6-network-pressure` alerts (`NodeConntrackHighWarn/Critical/MetricMissing`); other collectors (CPU, disk, fs, network, load, OS info) are dropped at ServiceMonitor level. | ~4 per node |
| **kubelet** | Built-in (kubelet ServiceMonitor) | **Currently 0 series — `kubelet.enabled: false`** in `modules/monitoring/helm/values.yaml` (IPv6 migration). Planned post-IPv6 state: whitelisted to `kubelet_running_pods`, `kubelet_running_containers`, `kubelet_node_name`; `/metrics/probes` disabled. | 0 per node (kubelet SM off) |
| **cAdvisor** | Built-in (kubelet) | **Currently 0 series — kubelet ServiceMonitor disabled** (see kubelet row above). Planned post-IPv6 state: whitelisted to `container_memory_working_set_bytes` and `container_memory_rss`, then restricted by Alloy `cost_control` to control-plane namespaces only (`arc-systems\|karpenter\|harbor-system\|monitoring\|logging\|buildkit`). Runner nodes will contribute zero cAdvisor series even after re-enablement. | 0 per node (kubelet SM off) |
| **kube-proxy** | DaemonSet (all nodes, EKS-managed) | Not scraped — no ServiceMonitor for `kube-proxy` metrics; logs collected via base pipeline. | 0 per node |
| **aws-node (VPC-CNI)** | DaemonSet (all nodes, EKS-managed) | Not scraped — no ServiceMonitor; logs collected via base pipeline. | 0 per node |
| **nodelocaldns** | DaemonSet (all nodes) | Two scrape ports: `:9253` exposes CoreDNS plugin metrics per zone (4 zones — `cluster.local`, `in-addr.arpa`, `ip6.arpa`, `.`) — request counts by type/proto/family, response codes, cache hits/misses/entries, forward stats, plus `_sum`/`_count` for duration histograms. `:9353` exposes the binary's `coredns_nodecache_setup_errors_total` (single series). Drops `go_*`, `process_*`, and `coredns_*_request_duration_seconds_bucket` at PodMonitor level (matches `coredns.yaml` convention). | ~40-60 per node (~10-15 per zone × 4 zones, plus 1 setup-errors series) |
| **DCGM exporter** | DaemonSet (GPU nodes only) | 25 curated GPU metrics per GPU (Tier 1: 22, Tier 2: 3): utilization, memory, temp, power, ECC, XID errors, NVLink, PCIe, clocks, throttling | 25 per GPU |

**Per-node totals** (varies by node type — see the stale-estimates warning at the top of this doc; the numbers below are pre-whitelist and overstate cardinality by 1–2 orders of magnitude. cAdvisor column is **currently 0 everywhere** because the kubelet ServiceMonitor is disabled for the IPv6 migration):

| Node type | node-exporter | cAdvisor | nodelocaldns | DCGM | Total per node |
|-----------|:---:|:---:|:---:|:---:|:---:|
| CPU runner node (~10 containers) | ~300 | 0 (SM off) | ~50 | 0 | ~350 |
| GPU node (8x GPUs, ~10 containers) | ~300 | 0 (SM off) | ~50 | ~208 | ~558 |
| Base infra node (~30 containers) | ~300 | 0 (SM off) | ~50 | 0 | ~350 |

### Cluster-Wide (Fixed per Source)

These metrics come from centralized Deployments or StatefulSets. Series counts below are **per replica** unless noted. Total cluster overhead = sum of (per-replica rate × replica count) for each source.

| Source | What's collected | ~Series per replica |
|--------|-----------------|---------------------|
| **K8s API server** (EKS-managed) | Whitelisted to `apiserver_request_total` and `apiserver_request_terminations_total` only. | small fixed set (whitelist of 2 metric families) |
| **CoreDNS** (EKS-managed) | kube-prometheus-stack built-in `coreDns` ServiceMonitor is disabled (`coreDns.enabled: false`); custom PodMonitor (`modules/monitoring/kubernetes/monitors/podmonitors/coredns.yaml`) scrapes the pods directly. Keeps: request/response counts, latency (sum/count), cache hits/misses/entries, forward request stats. Drops: `go_*`, `process_*`, and `coredns_dns_request_duration_seconds_bucket|coredns_forward_request_duration_seconds_bucket` at the PodMonitor level. | ~30-50 per replica |
| **kube-state-metrics** | K8s object state: pod/deployment/node/daemonset/statefulset/PV/HPA/job/namespace status. Two-layer filter: (1) `--metric-allowlist=kube_(daemonset\|deployment\|pod\|namespace\|node\|statefulset\|persistentvolume\|horizontalpodautoscaler\|job)_.+` extraArg controls which resource groups KSM generates; (2) per-monitor `keep` allowlist further narrows daemonset/deployment/pod metrics (e.g. only `kube_daemonset_status_(desired_number_scheduled\|number_ready\|number_available\|number_unavailable)`, `kube_deployment_status_(replicas_ready\|replicas_available\|replicas_unavailable\|condition)`, `kube_pod_info\|kube_pod_container_status_*\|kube_pod_status_reason`) plus three `drop` rules for benign Completed terminations and routine pod status reasons. replicaset NOT included. | ~200-500 total (single replica) |
| **Karpenter controller** | Whitelisted to 7 metric families: `karpenter_nodeclaims_created_total`, `karpenter_nodes_created_total`, `karpenter_nodes_terminated_total`, `karpenter_nodepools_usage`, `karpenter_nodepools_limit`, `karpenter_nodes_allocatable`, `karpenter_interruption_received_messages_total`. No latency, histograms, or disruption counts. | small fixed set per replica |
| **ARC controller** | Whitelisted to `gha_controller_.*` and `controller_runtime_reconcile_errors_total`. | ~10-25 per replica |
| **Harbor exporter** | No positive whitelist — only `go_*\|process_*\|promhttp_*` are dropped at the ServiceMonitor level. Actual cardinality is the full Harbor metric set; estimate is approximate. | ~50-100 per replica (unverified upper bound) |
| **BuildKit daemon** | Drops `go_*`, `process_*`, `promhttp_*`, `target_info`, and all `_bucket` histograms; no positive whitelist. Cardinality could be higher than this estimate depending on remaining `_sum/_count` series. | ~6-12 per replica (unverified) |
| **BuildKit HAProxy** | Whitelisted to 4 metrics only: `haproxy_server_status`, `haproxy_server_current_sessions`, `haproxy_server_connection_errors_total`, `haproxy_backend_current_sessions`. No bytes in/out, rates, or queue depth. | small fixed set per replica |
| **node-compactor** | Compaction cycle counts, nodes tainted/untainted, utilization, burst events | ~10-20 total (single replica) |
| **Pushgateway** | Hosts metrics pushed by CronJobs (zombie-cleanup, etc.). `honorLabels: true`. Volume depends on which jobs push and how many label combinations they emit. | varies (push-based, job-dependent) |
| **pypi-cache nginx** | Whitelisted to `nginx_up`, `nginx_http_requests_total`, `nginx_connections_active` per nginx-exporter sidecar (one per pod — 4 deployments × N replicas per cluster). | small fixed set per pod |

### Scaling Formula (Metrics)

> **NOTE:** the per-node and per-pod multipliers below are stale (see warning at top of doc). They predate the metric whitelisting work — node-exporter is now ~4 series per node (not ~300; memory 2 + conntrack 2) and runner-pod cAdvisor is currently 0 series (kubelet ServiceMonitor disabled for IPv6 migration). The DCGM term should use 25, not 26 (the curated metrics CSV defines exactly 25 metrics). The nodelocaldns term (~50 per node) is post-whitelist and stable. Use the formula structure but expect totals to be 1–2 orders of magnitude lower in practice.

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
- The `N_scale_sets × 85` term reflects the listener's full per-scale-set series count: ~5-10 baseline (`gha_assigned_jobs`, `gha_completed_jobs_total`, `gha_started_jobs_total`, `gha_running_jobs`, job-duration sum/count) plus ~77 from the 16 `gha_capacity_*` families (the two histograms — `gha_capacity_reconcile_duration_seconds` and `gha_capacity_hud_request_duration_seconds` — contribute ~22 each via 9 buckets + sum + count across two label values; `gha_capacity_placeholder_pods` contributes 10 via the `role`(2)×`phase`(5) cross-product; the remaining gauges/counters add the rest). Note: only the job-duration histograms (`gha_job_(execution|startup)_duration_seconds_bucket`) are dropped by Alloy `cost_control`; the two capacity histograms above survive and contribute their full bucket × label cross-products.

### What's NOT Collected (Metrics)

| Source | Status | Notes |
|--------|--------|-------|
| etcd metrics | Not collected | EKS manages etcd; not exposed |
| CloudWatch / VPC / EBS metrics | Not collected | No CloudWatch exporter. Control plane logs go to CloudWatch (see Terraform) |
| kube-proxy metrics | Not scraped | Low signal-to-noise for CI workloads. Logs collected via logging pipeline |
| kubelet metrics (non-cAdvisor) | Disabled (IPv6 migration) | kube-prometheus-stack `kubelet.enabled: false`. Planned post-IPv6 state: whitelisted to `kubelet_running_pods`, `kubelet_running_containers`, `kubelet_node_name`; `/metrics/probes` disabled. See `observability.md` "Disabled scrapers (IPv6 migration)". |
| kube-controller-manager metrics | Not collected | EKS does not expose controller-manager endpoints |
| kube-scheduler metrics | Not collected | EKS does not expose scheduler endpoints |

## Log Volume Estimation Reference

Only two log sources ship to Grafana Cloud Loki:

1. **System journal** — per-node DaemonSet, scoped to a fixed `keep` filter (`kubelet.service|containerd.service|kernel|nvidia-fabricmanager.service|nvidia-persistenced.service`)
2. **Kubernetes events** — single `alloy-events` Deployment watching the K8s Events API across all namespaces except `logging`

Container stdout/stderr is **not** shipped. Anything below assumes the journal + events architecture only.

### Per Node (Journal Overhead)

| Source | ~Lines/min per node | Notes |
|--------|:---:|-------|
| **kubelet** (journal) | 30-100 | Pod lifecycle, volume mounts, probes |
| **containerd** (journal) | 20-80 | Container start/stop, image pulls |
| **kernel** (journal) | 1-10 | OOM kills, hardware errors |
| **nvidia-fabricmanager** (journal) | 1-5 | GPU nodes only |
| **nvidia-persistenced** (journal) | 0-2 | GPU nodes only |

Priority-7 (`debug`) entries are dropped (`drop_counter_reason="journal_debug"`).

Per-node totals: **~55-200 lines/min** = **~0.02-0.07 GB/day** per node. Identical for CPU and GPU runner nodes, base nodes, and BuildKit nodes — journal volume is bounded by the per-unit `keep` filter, not by pod density on the node.

### Kubernetes Events

Single `alloy-events` Deployment. Event volume scales with cluster activity, not replica count.

| Condition | ~Events/min | Notes |
|-----------|:---:|-------|
| Steady state (few pod changes) | 10-50 | Baseline cluster activity |
| Active scaling (runner pods churning) | 100-500 | Proportional to pod churn rate |
| Spike (mass scheduling event) | 500-2,000 | Transient |

Events are JSON (~700 bytes average). Fields extracted: `type` and `kind` to indexed labels; `reason` and `sourcecomponent` to structured metadata.

### Scaling Formula (Logs)

```
Total GB/day ≈ N_total_nodes × 0.02-0.07 GB     (journal overhead)
             + events_GB                         (cluster activity)
```

Where `events_GB` depends on cluster activity level (see events table above). Both terms are bounded and predictable — there is no per-pod or per-job log multiplier any more.

### What's NOT Sent to Loki

| Source | Why not collected |
|--------|-------------------|
| Container stdout/stderr (any namespace) | Intentionally not shipped — runner-job volume was too costly; GitHub Actions stores full workflow logs |
| Alloy pods (`logging` namespace) | Self-excluded at the events source (feedback loop prevention) |
| kube-apiserver / kube-scheduler / kube-controller-manager / etcd | EKS-managed; not exposed as journal units |
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
| Base infra nodes | 6 | `clusters.yaml` defaults → `base_node_count: 6` (m7i.12xlarge per `base_node_instance_type`; `arc-cbr-production` has no override) |
| Runner scale sets | 50 | 42 upstream (`modules/arc-runners/defs/`) + 4 B200 (`modules/arc-runners-b200/defs/`) + 4 H100 (`modules/arc-runners-h100/defs/`) |

From `clusters.yaml` defaults plus `arc-cbr-production` overrides:

| Service | Replicas | Source |
|---|---|---|
| Karpenter | 4 | `defaults.karpenter.replicas: 4` |
| ARC controller | 4 | `defaults.arc.replica_count: 4` |
| Harbor nginx | 6 | `defaults.harbor.nginx_replicas: 6` |
| Harbor core | 4 | `defaults.harbor.core_replicas: 4` |
| Harbor registry | 4 | `defaults.harbor.registry_replicas: 4` |
| Harbor exporter / jobservice / portal / db / redis | 1 each | chart defaults |
| node-compactor | 1 | single replica |
| Monitoring Alloy | 2 | Hard-coded in `modules/monitoring/helm/alloy-values.yaml` (`controller.replicas: 2`, `clustering.enabled: true`) — there is no `monitoring.alloy_replicas` config knob |
| BuildKit daemon | 10 | `defaults.buildkit.replicas_per_arch: 5` × 2 archs |
| BuildKit HAProxy | 1 | single replica |
| CoreDNS | 6 | `defaults.coredns.replicas: 6` (`arc-cbr-production` has no override) |
| kube-state-metrics | 1 | single replica |
| Prometheus Operator | 1 | single replica |
| pypi-cache nginx | 40 | 4 deployments per CUDA slug (`cpu`, `cu126`, `cu128`, `cu130`) × `arc-cbr-production.pypi_cache.replicas: 10` |

### Cluster-wide fixed overhead (C_fixed)

Using midpoint of per-replica ranges from the [cluster-wide reference table](#cluster-wide-fixed-per-source):

| Source | ~Series/replica (mid) | Replicas | Subtotal |
|---|---|---|---|
| K8s API server | ~300 | 1 | ~300 |
| CoreDNS | ~40 | 6 | ~240 |
| kube-state-metrics | ~350 | 1 | ~350 |
| Karpenter | ~38 | 4 | ~152 |
| ARC controller | ~18 | 4 | ~72 |
| Harbor exporter | ~75 | 1 | ~75 |
| BuildKit daemon | ~9 | 10 | ~90 |
| BuildKit HAProxy | ~23 | 1 | ~23 |
| node-compactor | ~15 | 1 | ~15 |
| pypi-cache nginx (per-pod) | ~5 | 40 | ~200 |
| **Total C_fixed** | | | **~1,517** |

### Peak cardinality calculation

Applying the [scaling formula](#scaling-formula-metrics). **The numerical totals below are stale** — they predate the metric whitelisting work (PR #418, #428) that landed after this calculation, and they use the old scale-set count (40 → now 50) and the off-by-one DCGM constant (26 → actual 25). The structure is preserved for reference; re-validate against live Mimir before relying on these numbers.

```
Total series ≈ C_fixed
             + N_runner_pods × 40
             + N_cpu_nodes × 715
             + N_gpu_nodes × 715 + 25 × N_total_GPUs
             + N_base_nodes × 1515
             + N_scale_sets × 85

           ≈ 1,622                              (refreshed C_fixed)
             + 7,367 × 40               =    294,680   (stale: per-pod cAdvisor currently 0 — kubelet SM disabled)
             + 680 × 715                =    486,200   (stale: per-node node-exporter now ~4; cAdvisor currently 0)
             + 1,433 × 715 + 25 × 2,989 =  1,099,320   (stale: per-node node-exporter now ~4; cAdvisor currently 0; H100 GPUs not counted)
             + 6 × 1,515               =      9,090   (stale: per-node multiplier overstated; base node count refreshed 5 → 6)
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

**Date:** 2026-03-24 (input data); per-source descriptions refreshed 2026-06-25. **Scope:** pytorch/pytorch self-hosted Linux runners only (same scope as the fleet simulation). Actual volume will also be higher due to unmapped runner types, B200/H100 GPU runners, and other repos sharing the cluster.

### Input data

From the load distribution data:

| Parameter | Value | Source |
|---|---|---|
| Peak fleet | 2,113 nodes | Fleet simulation |
| Weekday avg fleet | ~1,355 nodes | Derived from [node churn estimates](current_runner_load_distribution.md#fleet-size-by-time-of-day) |
| Weekend avg fleet | ~249 nodes | [Node churn estimates](current_runner_load_distribution.md#fleet-size-by-time-of-day) |
| BuildKit nodes | 5 | `clusters.yaml` arc-cbr-production override → `buildkit.pods_per_node: 2` |
| Base nodes | 6 | `clusters.yaml` defaults → `base_node_count: 6` |

### Component breakdown (weekday average)

#### Journal overhead (per node)

Every node — runner, BuildKit, base — produces 0.02–0.07 GB/day (midpoint ~0.045) in journal logs. The rate is the same regardless of pod density on the node because journal scope is fixed (`kubelet|containerd|kernel|nvidia-*`).

| Load level | Total nodes (runner + BuildKit + base) | GB/day (low) | GB/day (mid) | GB/day (high) |
|---|---|---|---|---|
| Weekday average | ~1,366 (1,355 + 5 + 6) | 27.3 | 61.5 | 95.6 |
| Weekend average | ~260 (249 + 5 + 6) | 5.2 | 11.7 | 18.2 |

#### Kubernetes events

Pod churn drives event volume. At ~53,300 jobs/day (~37 job starts/min average), plus DaemonSet scheduling and node lifecycle events:

| Cluster activity | Est. events/min | GB/day |
|---|---|---|
| Average (weekday) | ~200 | ~0.20 |
| Peak (ramp-up/scale-down) | ~500 | ~0.50 |

### Total daily volume estimate (weekday average)

| Component | GB/day | % of Total |
|---|---|---|
| Journal overhead (all nodes) | ~62 | ~99.7% |
| Events | ~0.2 | ~0.3% |
| **Total** | **~62** | |

Monthly total: **~1,860 GB** (weekday rate; actual monthly will be ~15–20% lower due to reduced weekend load).

### Caveats

1. **Journal overhead range**: the 0.02–0.07 GB/day/node range covers kubelet + containerd + kernel chatter. The high end assumes verbose kubelet logging during active pod scheduling. Sustained average is likely closer to the low end (0.02–0.03 GB/day/node) once nodes stabilize.

2. **Logging module gating**: log shipping requires `grafana-cloud-credentials` to exist in the `logging` namespace. Until the secret is present, no logs are sent at all.

3. **Container stdout/stderr is not shipped.** To debug container behavior: GitHub Actions stores full workflow logs for runner jobs, `kubectl logs` works while a pod is alive, and pod events (scheduling, OOM, restarts) are queryable in Loki via the `alloy-events` Deployment.

