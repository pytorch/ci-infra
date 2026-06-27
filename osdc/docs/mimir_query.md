# Querying Grafana Cloud Mimir (Prometheus Metrics)

## Context

The OSDC clusters ship metrics to Grafana Cloud Mimir via Grafana Alloy. There is no in-cluster Prometheus — Alloy scrapes metrics locally and remote-writes them to Mimir. This doc covers how to query Mimir from the CLI as an agent.

## Prerequisites

- `kubectl` access to the target cluster (managed by mise in the `osdc/` directory)
- `jq` for parsing responses
- The `grafana-cloud-read-credentials` secret must exist in the `monitoring` namespace (see "Creating the read-credentials secret" below)

## Step 1 — Extract Credentials

Every query session starts by pulling credentials from the cluster. These cannot be cached across Bash tool calls, so **include them at the top of every curl command block**.

```bash
MIMIR_USER=$(NO_PROXY="$NO_PROXY,.eks.amazonaws.com" no_proxy="$no_proxy,.eks.amazonaws.com" \
  mise exec -- kubectl get secret grafana-cloud-read-credentials -n monitoring \
  -o jsonpath='{.data.username}' | base64 -d)

MIMIR_PASS=$(NO_PROXY="$NO_PROXY,.eks.amazonaws.com" no_proxy="$no_proxy,.eks.amazonaws.com" \
  mise exec -- kubectl get secret grafana-cloud-read-credentials -n monitoring \
  -o jsonpath='{.data.password}' | base64 -d)

MIMIR_URL="https://prometheus-prod-36-prod-us-west-0.grafana.net/api/prom"
```

**Important**: The `NO_PROXY` / `no_proxy` bypass is required because of Meta's corporate proxy. Without it, `kubectl` calls to EKS will fail.

**Note**: There are two secrets — `grafana-cloud-credentials` (write, used by Alloy) and `grafana-cloud-read-credentials` (read, for queries). Always use the **read** secret for queries.

The Mimir URL comes from `clusters.yaml` under `monitoring.grafana_cloud_read_url`.

### Creating the read-credentials secret

The write-side `grafana-cloud-credentials` secret is documented in `docs/observability.md` ("Credential setup (monitoring)"). The read-side secret is created the same way but with a read-scope API key:

```bash
kubectl create secret generic grafana-cloud-read-credentials \
  -n monitoring \
  --from-literal=username='<GRAFANA_CLOUD_METRICS_USER_ID>' \
  --from-literal=password='<API_KEY_WITH_METRICS_READ_SCOPE>'
```

Neither secret is created by `deploy.sh` — both are operator-provided per cluster.

## Step 2 — Query

Mimir exposes a Prometheus-compatible HTTP API. Two main endpoints:

| Endpoint | Purpose |
|----------|---------|
| `$MIMIR_URL/api/v1/query` | Instant query (current value) |
| `$MIMIR_URL/api/v1/query_range` | Range query (time series) |

### Combined Template (copy-paste ready)

**Instant query** — returns the current value:

```bash
MIMIR_USER=$(NO_PROXY="$NO_PROXY,.eks.amazonaws.com" no_proxy="$no_proxy,.eks.amazonaws.com" \
  mise exec -- kubectl get secret grafana-cloud-read-credentials -n monitoring \
  -o jsonpath='{.data.username}' | base64 -d) && \
MIMIR_PASS=$(NO_PROXY="$NO_PROXY,.eks.amazonaws.com" no_proxy="$no_proxy,.eks.amazonaws.com" \
  mise exec -- kubectl get secret grafana-cloud-read-credentials -n monitoring \
  -o jsonpath='{.data.password}' | base64 -d) && \
MIMIR_URL="https://prometheus-prod-36-prod-us-west-0.grafana.net/api/prom" && \
curl -s -u "$MIMIR_USER:$MIMIR_PASS" \
    "$MIMIR_URL/api/v1/query" \
    --data-urlencode 'query=up{cluster="meta-prod-aws-ue2"}' | jq .
```

**Range query** — returns a time series:

```bash
# ... (credential extraction) ... && \
curl -s -u "$MIMIR_USER:$MIMIR_PASS" \
    "$MIMIR_URL/api/v1/query_range" \
    --data-urlencode 'query=sum(node_memory_MemAvailable_bytes{cluster="meta-prod-aws-ue2"})' \
    --data-urlencode "start=$(date -u -v-1H +%s)" \
    --data-urlencode "end=$(date -u +%s)" \
    --data-urlencode "step=60" | jq .
```

### Time Range Parameters (range queries)

| Parameter | Format | Description |
|-----------|--------|-------------|
| `start` | Unix timestamp (seconds) | Start of range |
| `end` | Unix timestamp (seconds) | End of range |
| `step` | Seconds | Resolution (60 = one point per minute) |

Common lookback expressions (macOS `date`):

| Lookback | Expression |
|----------|-----------|
| 1 hour | `$(date -u -v-1H +%s)` |
| 6 hours | `$(date -u -v-6H +%s)` |
| 24 hours | `$(date -u -v-24H +%s)` |
| 7 days | `$(date -u -v-7d +%s)` |
| Now | `$(date -u +%s)` |

## Available Scrape Jobs

These are the scrape jobs configured in the cluster. **Most jobs apply tight `keep` allowlists at scrape time** — see `modules/monitoring/CLAUDE.md` for the three filtering layers (`--metric-allowlist` → `metricRelabelings` → Alloy `cost_control`). Querying a metric not in the keep allowlist will silently return zero results.

Target counts vary continuously with cluster size and are not listed here — query `count(up{cluster="...", job="<job>"})` for live counts.

> **Kubelet / cAdvisor metrics are unavailable.** Under IPv6-only EKS, the kubelet ServiceMonitor (`kubelet.enabled: false` in `modules/monitoring/helm/values.yaml`) is disabled — Prometheus Operator's kubelet Endpoints picker returns the IPv4 NodeInternalIP, which Alloy (IPv6-only pod) cannot reach. **All of `kubelet_running_pods`, `kubelet_running_containers`, `container_memory_working_set_bytes`, and `container_memory_rss` return empty in current clusters.** See `docs/observability.md` ("Disabled scrapers (IPv6 migration)") for the re-enable plan. Until a custom IPv6-aware kubelet ServiceMonitor ships, treat any query against these metric names as broken-by-design — not as a Mimir failure.

The table below lists the **ServiceMonitor / PodMonitor name** alongside the **actual `job=` label** that appears in Mimir. They differ because ServiceMonitors inherit the `job` label from the target *Service* name, not the monitor name.

| Monitor name | Actual `job=` label | Description | Filter notes |
|--------------|---------------------|-------------|--------------|
| `node-exporter` (built-in) | `node-exporter` | Node-level memory + conntrack only | Only `node_memory_MemAvailable_bytes`, `node_memory_MemTotal_bytes`, `node_nf_conntrack_entries`, and `node_nf_conntrack_entries_limit` are kept; CPU/disk/network are dropped at scrape. Conntrack metrics power `NodeConntrackHigh{Warn,Critical}` alerts (critical under IPv6-only EKS where pod-IPv4-internet egress hits VPC CNI SNAT). |
| `kubelet` (built-in) | — (DISABLED) | Would emit `kubelet_running_(pods\|containers)` and `kubelet_node_name` | ServiceMonitor disabled on IPv6-only EKS — queries return empty. |
| `kubelet` cAdvisor (built-in) | — (DISABLED) | Would emit `container_memory_working_set_bytes` and `container_memory_rss` | ServiceMonitor disabled on IPv6-only EKS — queries return empty. (Alloy's `cost_control` two-step replace+drop targeting these metrics is still configured but has no source data to filter.) |
| `kube-state-metrics` (built-in) | `kube-state-metrics` | Filtered KSM allowlist | Keeps `kube_pod_info`, `kube_pod_container_status_*` (both `last_terminated_reason` AND `terminated_reason` — the non-`last_` variant fires for Job/CronJob containers with `restartPolicy: Never`), `kube_pod_status_reason`, `kube_deployment_status_*`, daemonset/namespace/node/statefulset/pv/hpa/job metrics. `Completed` reason and exit code `0` are dropped from both terminated variants. |
| `apiserver` | `kubernetes` | API server health | Only `apiserver_request_total` and `apiserver_request_terminations_total` are kept |
| `coredns` PodMonitor | `monitoring/coredns` | DNS resolution metrics | All `coredns_*` except buckets, plus `go_*`/`process_*` dropped |
| `nodelocaldns` PodMonitor | `monitoring/nodelocaldns` | Per-node NodeLocal DNSCache (NLD) DaemonSet metrics; scrapes both ports `9253` (CoreDNS plugin) and `9353` (binary `coredns_nodecache_setup_errors_total`) at 60s | All `coredns_*` except `coredns_*_request_duration_seconds_bucket`, plus `go_*`/`process_*` dropped |
| `karpenter` | `karpenter` | Autoscaler counters and capacity | Only `karpenter_nodeclaims_created_total`, `karpenter_nodes_created_total`, `karpenter_nodes_terminated_total`, `karpenter_nodepools_usage`, `karpenter_nodepools_limit`, `karpenter_nodes_allocatable`, `karpenter_interruption_received_messages_total` |
| `arc-listeners` PodMonitor | `monitoring/arc-listeners` | ARC runner-scale-set listener metrics | Keeps `gha_assigned_jobs`, `gha_completed_jobs_total`, `gha_started_jobs_total`, `gha_running_jobs`, `gha_job_*_duration_seconds_(sum\|count)`, `gha_capacity_*` |
| `arc-controller` | (varies — Service name depends on ARC chart version) | ARC controller metrics | Keeps `gha_controller_.*` and `controller_runtime_reconcile_errors_total` only |
| `harbor` | (varies — Harbor exporter emits per-component job labels: `core`, `registry`, `jobservice`, etc.) | Harbor registry exporter | All metrics except `go_*`/`process_*`/`promhttp_*` |
| `buildkit` | `buildkitd-pods` | BuildKit pod metrics | Drops `go_*`, `process_*`, `promhttp_*`, `target_info`, AND `.*_bucket` (all histogram buckets). For latency, use `rate(metric_sum)/rate(metric_count)` — `histogram_quantile(...)` will not work. |
| `buildkit-haproxy` | `buildkitd-lb-metrics` | BuildKit LB metrics | (See ServiceMonitor for filter) |
| `node-compactor` | `node-compactor` | Node consolidation/compactor metrics | No filter |
| `pushgateway` | `pushgateway` | Prometheus Pushgateway (ad-hoc job metrics) | No filter; `honorLabels: true` — pushed metrics' `job`/`instance` labels are preserved instead of overwritten by scrape target |
| `pypi-cache` | (one job per Service per CUDA slug — e.g. `pypi-cache-cpu`, `pypi-cache-cu126`, `pypi-cache-cu128`, `pypi-cache-cu130` per `clusters.yaml` `cuda_versions`) | PyPI cache nginx metrics | Only `nginx_up`, `nginx_http_requests_total`, `nginx_connections_active` |
| `dcgm-exporter` | `dcgm-exporter` | NVIDIA GPU metrics (only on GPU nodes) | All `DCGM_FI_*` metrics flow through (no metric drops); label drops only: `UUID`, `modelName`, `DCGM_FI_DRIVER_VERSION`, `pci_bus_id` |

In addition, the `kube-prometheus-stack-operator` job exists but Alloy's `cost_control` drops every `prometheus_operator_*` metric (and `go_*`/`process_*`/`promhttp_*`), so only the `up` metric is queryable in practice.

Job naming follows two conventions:

- **ServiceMonitors**: the `job` label is the target Service name (e.g. `karpenter`, `kubernetes` for the API server, `buildkitd-pods` for BuildKit). This is why the "Monitor name" and "Actual `job=` label" columns above often differ.
- **PodMonitors**: the `job` label is `<namespace>/<podmonitor-name>` (e.g. `monitoring/arc-listeners`, `monitoring/coredns`).

Confirm exact job names by running `count(up{cluster="..."}) by (job)`.

## Key Labels

All metrics include `cluster="meta-prod-aws-ue2"` (set by Alloy as an external label). Other common labels:

| Label | Example | Notes |
|-------|---------|-------|
| `namespace` | `arc-runners`, `kube-system` | Kubernetes namespace |
| `pod` | `runner-xyz-abc` | Pod name |
| `container` | `runner` | Container name |
| `node` | `ip-10-4-154-0.us-east-2.compute.internal` | Node hostname (use `kube_pod_info` to map pod → node) |
| `instance` | `[fd00:ec2::xxx]:9100` | Scrape target address. **IPv6 under IPv6-only EKS** — node-exporter binds the node's IPv6 hostIP via Downward API (`listenOnAllInterfaces: false`). |
| `job` | `node-exporter`, `karpenter` | Scrape job name |
| `nodepool` | `c7i-12xlarge`, `g5-48xlarge` | **Only present on Karpenter and node-compactor metrics**, NOT a global label |

## Example Queries

> **IMPORTANT**: After PR #428 (2026-04-08) tightened the metric allowlist, many "obvious" PromQL queries return zero results because the source metric is dropped at scrape time. The examples below only use metrics that actually pass through the three filtering layers. See `modules/monitoring/CLAUDE.md` for the layer breakdown, and the "Available Scrape Jobs" table above for what each job emits.

### Cluster Overview

```promql
# Node count
count(kube_node_info{cluster="meta-prod-aws-ue2"})

# Pod count by namespace
count(kube_pod_info{cluster="meta-prod-aws-ue2"}) by (namespace)

# Cluster-wide memory available (bytes)
sum(node_memory_MemAvailable_bytes{cluster="meta-prod-aws-ue2"})

# Cluster-wide memory usage %
100 * (1 - sum(node_memory_MemAvailable_bytes{cluster="meta-prod-aws-ue2"}) / sum(node_memory_MemTotal_bytes{cluster="meta-prod-aws-ue2"}))

# Up targets by job
count(up{cluster="meta-prod-aws-ue2"}) by (job)
```

> **Note**: CPU, disk, and most network metrics from `node_exporter` are dropped at scrape. Only `node_memory_MemAvailable_bytes`, `node_memory_MemTotal_bytes`, `node_nf_conntrack_entries`, and `node_nf_conntrack_entries_limit` are kept. For per-pod CPU you need a different source (e.g. KSM pod resource metrics) — `node_cpu_seconds_total`, `node_filesystem_*`, and `node_network_*` will return empty.

### Node Metrics

```promql
# Memory available per node (bytes)
node_memory_MemAvailable_bytes{cluster="meta-prod-aws-ue2"}

# Memory usage per node (%)
100 * (1 - node_memory_MemAvailable_bytes{cluster="meta-prod-aws-ue2"} / node_memory_MemTotal_bytes{cluster="meta-prod-aws-ue2"})

# Conntrack table utilization per node (%)
100 * node_nf_conntrack_entries{cluster="meta-prod-aws-ue2"} / node_nf_conntrack_entries_limit{cluster="meta-prod-aws-ue2"}

# Nodes with conntrack pressure (matches NodeConntrackHigh alert threshold)
node_nf_conntrack_entries{cluster="meta-prod-aws-ue2"} / node_nf_conntrack_entries_limit{cluster="meta-prod-aws-ue2"} > 0.80
```

> **Kubelet metrics unavailable**: `kubelet_running_pods` and `kubelet_running_containers` return empty in current clusters — the kubelet ServiceMonitor is disabled under IPv6-only EKS. See the warning at the top of "Available Scrape Jobs".

### Pod / Container Health

```promql
# Pod-to-node mapping (use to join pod-level metrics with node info)
kube_pod_info{cluster="meta-prod-aws-ue2"}

# Container restarts in last 24h — control-plane namespaces only
# (kube_pod_container_status_restarts_total is filtered to arc-systems|karpenter|harbor-system|monitoring|logging|buildkit by Alloy)
topk(10, increase(kube_pod_container_status_restarts_total{cluster="meta-prod-aws-ue2"}[24h]))

# Pods that exited with a non-Completed reason (last terminated)
kube_pod_container_status_last_terminated_reason{cluster="meta-prod-aws-ue2"}

# Pods that exited with non-zero exit code
kube_pod_container_status_last_terminated_exitcode{cluster="meta-prod-aws-ue2", container_exit_code!="0"}

# Pod status reasons (Evicted, NodeLost, UnexpectedAdmissionError — routine reasons are dropped)
kube_pod_status_reason{cluster="meta-prod-aws-ue2"} == 1
```

> **Note**: `kube_pod_status_phase` is NOT in the KSM keep allowlist — phase-based queries return nothing. Use `kube_pod_info` joined with `kube_pod_container_status_*` instead. `container_memory_working_set_bytes`, `container_memory_rss`, and `container_cpu_usage_seconds_total` all return empty in current clusters — the kubelet/cAdvisor ServiceMonitor is disabled under IPv6-only EKS. The Alloy `cost_control` two-step replace+drop pattern that previously scoped cAdvisor memory to control-plane namespaces is still configured but has no source data to filter; queries against these metrics will be empty until a custom IPv6-aware kubelet ServiceMonitor ships.

### Karpenter (Autoscaling)

```promql
# NodePool usage (current allocation per nodepool)
karpenter_nodepools_usage{cluster="meta-prod-aws-ue2"}

# NodePool limits
karpenter_nodepools_limit{cluster="meta-prod-aws-ue2"}

# Allocatable capacity per Karpenter-managed node
karpenter_nodes_allocatable{cluster="meta-prod-aws-ue2"}

# Nodes created in the last hour
increase(karpenter_nodes_created_total{cluster="meta-prod-aws-ue2"}[1h])

# Nodes terminated in the last hour
increase(karpenter_nodes_terminated_total{cluster="meta-prod-aws-ue2"}[1h])

# NodeClaims created (provisioning attempts)
increase(karpenter_nodeclaims_created_total{cluster="meta-prod-aws-ue2"}[1h])

# Spot interruptions received in the last hour
increase(karpenter_interruption_received_messages_total{cluster="meta-prod-aws-ue2"}[1h])
```

> **Note**: Only the seven karpenter metrics above are kept by the Karpenter ServiceMonitor. `karpenter_nodepools_allowed_disruptions`, `karpenter_nodes_total`, and `karpenter_provisioner_scheduling_duration_seconds` will return empty.

### ARC Listeners (Runner Scale Sets)

```promql
# Currently assigned jobs per runner scale set
gha_assigned_jobs{cluster="meta-prod-aws-ue2"}

# Currently running jobs per runner scale set
gha_running_jobs{cluster="meta-prod-aws-ue2"}

# Job completion rate (last hour)
rate(gha_completed_jobs_total{cluster="meta-prod-aws-ue2"}[1h])

# Job start rate (last hour)
rate(gha_started_jobs_total{cluster="meta-prod-aws-ue2"}[1h])

# Average job execution duration (sum/count — buckets dropped)
rate(gha_job_execution_duration_seconds_sum{cluster="meta-prod-aws-ue2"}[1h])
  / rate(gha_job_execution_duration_seconds_count{cluster="meta-prod-aws-ue2"}[1h])
```

### Control Plane

```promql
# API server request rate by verb
sum by (verb) (rate(apiserver_request_total{cluster="meta-prod-aws-ue2"}[5m]))

# API server terminations (rate-limited / dropped requests)
rate(apiserver_request_terminations_total{cluster="meta-prod-aws-ue2"}[5m])

# CoreDNS request rate
rate(coredns_dns_requests_total{cluster="meta-prod-aws-ue2"}[5m])
```

### NodeLocal DNSCache (NLD)

Per-node DaemonSet — each pod emits CoreDNS plugin metrics on port `9253` (zone-scoped request/cache counters) and the binary's own `coredns_nodecache_setup_errors_total` on port `9353`. The PodMonitor labels both ports with `job="monitoring/nodelocaldns"` and `pod=<nld-pod-name>` (one per node, host-network).

> **Metric name gotcha**: the binary's setup-error counter is **`coredns_nodecache_setup_errors_total`** (namespace `coredns`, subsystem `nodecache`), **NOT** `nodelocaldns_setup_errors_total`. Some upstream/internal docs and PR drafts use the wrong name. Queries against `nodelocaldns_setup_errors_total` will silently return zero results — always use the `coredns_nodecache_*` form.

> **Two CoreDNS request-count metric names exist** and the choice depends on which CoreDNS you're querying. NLD ships `k8s-dns-node-cache:1.26.8` which embeds an older CoreDNS that emits the legacy **`coredns_dns_request_count_total`**. The cluster CoreDNS (AWS-managed addon) is newer and emits **`coredns_dns_requests_total`** (used in the Control Plane section above). Do not "normalize" these to a single name — each is correct for its source.

```promql
# Cluster-wide NLD QPS across all zones (one pod per node)
sum(rate(coredns_dns_request_count_total{cluster="meta-prod-aws-ue2", job="monitoring/nodelocaldns"}[5m]))

# Per-zone NLD QPS (cluster.local.:53, in-addr.arpa.:53, ip6.arpa.:53, .:53)
sum by (server) (rate(coredns_dns_request_count_total{cluster="meta-prod-aws-ue2", job="monitoring/nodelocaldns"}[5m]))

# Per-node NLD QPS (top 10 noisiest nodes)
topk(10, sum by (pod) (rate(coredns_dns_request_count_total{cluster="meta-prod-aws-ue2", job="monitoring/nodelocaldns"}[5m])))

# Cluster-wide cache hit ratio (success hits / (success hits + misses))
sum(rate(coredns_cache_hits_total{cluster="meta-prod-aws-ue2", job="monitoring/nodelocaldns", type="success"}[5m]))
  / (sum(rate(coredns_cache_hits_total{cluster="meta-prod-aws-ue2", job="monitoring/nodelocaldns", type="success"}[5m]))
     + sum(rate(coredns_cache_misses_total{cluster="meta-prod-aws-ue2", job="monitoring/nodelocaldns"}[5m])))

# Per-node cache hit ratio (find nodes with poor cache locality)
sum by (pod) (rate(coredns_cache_hits_total{cluster="meta-prod-aws-ue2", job="monitoring/nodelocaldns", type="success"}[5m]))
  / (sum by (pod) (rate(coredns_cache_hits_total{cluster="meta-prod-aws-ue2", job="monitoring/nodelocaldns", type="success"}[5m]))
     + sum by (pod) (rate(coredns_cache_misses_total{cluster="meta-prod-aws-ue2", job="monitoring/nodelocaldns"}[5m])))

# NLD setup errors (any non-zero value should fire NodeLocalDNSSetupErrors alert)
coredns_nodecache_setup_errors_total{cluster="meta-prod-aws-ue2"}

# Per-node NLD setup errors broken down by error type
sum by (pod, errortype) (coredns_nodecache_setup_errors_total{cluster="meta-prod-aws-ue2"})

# Number of NLD pods reporting metrics (should equal node count)
count(up{cluster="meta-prod-aws-ue2", job="monitoring/nodelocaldns"} == 1)
```

### PyPI Cache

```promql
# nginx availability per pypi-cache instance
nginx_up{cluster="meta-prod-aws-ue2"}

# Request rate
rate(nginx_http_requests_total{cluster="meta-prod-aws-ue2"}[5m])

# Active connections
nginx_connections_active{cluster="meta-prod-aws-ue2"}
```

### GPU (DCGM)

The DCGM ServiceMonitor doesn't drop any metric names — only labels (`UUID`, `modelName`, `DCGM_FI_DRIVER_VERSION`, `pci_bus_id`) — so the full `DCGM_FI_*` set is queryable on GPU nodes:

```promql
# GPU temperature per GPU (Celsius) — used by GPUTemperatureCritical alert (>95C for 5m)
DCGM_FI_DEV_GPU_TEMP{cluster="meta-prod-aws-ue2"}

# GPU utilization per GPU (%)
DCGM_FI_DEV_GPU_UTIL{cluster="meta-prod-aws-ue2"}

# Uncorrectable (double-bit) ECC errors — used by GPUDoublebitECCError alert
DCGM_FI_DEV_ECC_DBE_VOL_TOTAL{cluster="meta-prod-aws-ue2"}

# XID critical errors (48/79/94/95) — used by GPUXIDCriticalError alert
DCGM_FI_DEV_XID_ERRORS{cluster="meta-prod-aws-ue2"}
```

### Bad Node Detection (Join Pattern)

`kube_pod_info` provides pod-to-node mapping. Join with error metrics to find nodes producing failures:

```promql
count(
  kube_pod_container_status_last_terminated_exitcode{cluster="meta-prod-aws-ue2", container_exit_code!="0"}
  * on(namespace, pod) group_left(node) kube_pod_info{cluster="meta-prod-aws-ue2"}
) by (node)
```

## Compact Output Patterns

```bash
# Just values from an instant query
... | jq '.data.result[] | "\(.metric.job): \(.value[1])"'

# Just the single scalar value
... | jq '.data.result[0].value[1]'

# Metric names from a query
... | jq '[.data.result[] | .metric.__name__] | unique'

# Count of results
... | jq '.data.result | length'
```

## Discovering Metrics

To find what metrics exist for a particular component, use a regex match on `__name__`:

```bash
# Find all karpenter metrics
curl -s -u "$MIMIR_USER:$MIMIR_PASS" \
    "$MIMIR_URL/api/v1/query" \
    --data-urlencode 'query={cluster="meta-prod-aws-ue2", __name__=~"karpenter.*"}' \
    | jq '[.data.result[] | .metric.__name__] | unique'
```

**Warning**: Querying `label/__name__/values` can be very slow on Mimir with large cardinality. Use the `__name__=~"prefix.*"` approach above instead.

## Source

References:
- `CLAUDE.md` — general architecture
- `modules/monitoring/CLAUDE.md` — monitoring module docs, three filtering layers
- `clusters.yaml` — Mimir read/write URLs
- `modules/monitoring/helm/values.yaml` — kube-prometheus-stack scrape filters
- `modules/monitoring/helm/alloy-values.yaml` — Alloy `cost_control` rules + remote_write config
- `modules/monitoring/kubernetes/monitors/` — ServiceMonitor / PodMonitor metric allowlists

Verification stamp: doc setup/credential mechanics verified 2026-03-20. Example queries rewritten 2026-05-06 to reflect the metric allowlist tightening from PR #428 (2026-04-08). If you see queries returning empty results, cross-check the relevant ServiceMonitor / PodMonitor allowlist before assuming Mimir is broken.
