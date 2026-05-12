# Querying Grafana Cloud Mimir (Prometheus Metrics)

## Context

The OSDC clusters ship metrics to Grafana Cloud Mimir via Grafana Alloy. There is no in-cluster Prometheus — Alloy scrapes metrics locally and remote-writes them to Mimir. This doc covers how to query Mimir from the CLI as an agent.

## Prerequisites

- `kubectl` access to the target cluster (managed by mise in the `osdc/` directory)
- `jq` for parsing responses
- The `grafana-cloud-read-credentials` secret must exist in the `monitoring` namespace

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

The Mimir URL comes from `clusters.yaml` under `monitoring.grafana_cloud_mimir_read_url`.

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
    --data-urlencode 'query=up{cluster="pytorch-arc-cbr-production"}' | jq .
```

**Range query** — returns a time series:

```bash
# ... (credential extraction) ... && \
curl -s -u "$MIMIR_USER:$MIMIR_PASS" \
    "$MIMIR_URL/api/v1/query_range" \
    --data-urlencode 'query=sum(node_memory_MemAvailable_bytes{cluster="pytorch-arc-cbr-production"})' \
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

| Job | Description | Filter notes |
|-----|-------------|--------------|
| `node-exporter` | Node-level memory metrics only | Only `node_memory_MemAvailable_bytes` and `node_memory_MemTotal_bytes` are kept; CPU/disk/network are dropped at scrape |
| `kubelet` | Running pod/container counts only | Only `kubelet_running_(pods\|containers)` and `kubelet_node_name` are kept |
| `kubelet` (cAdvisor port) | Pod-level memory only | Only `container_memory_working_set_bytes` and `container_memory_rss` are kept; CPU/network/filesystem dropped. Alloy further restricts to control-plane namespaces (`arc-systems\|karpenter\|harbor-system\|monitoring\|logging\|buildkit`) |
| `kube-state-metrics` | Filtered KSM allowlist | Keeps `kube_pod_info`, `kube_pod_container_status_*`, `kube_pod_status_reason`, `kube_deployment_status_*`, daemonset/namespace/node/statefulset/pv/hpa/job metrics |
| `apiserver` | API server health | Only `apiserver_request_total` and `apiserver_request_terminations_total` are kept |
| `coredns` | DNS resolution metrics | All `coredns_*` except buckets, plus `go_*`/`process_*` dropped |
| `monitoring/nodelocaldns` | Per-node NodeLocal DNSCache (NLD) DaemonSet metrics; scrapes both ports `9253` (CoreDNS plugin) and `9353` (binary `coredns_nodecache_setup_errors_total`) at 60s | All `coredns_*` except `coredns_*_request_duration_seconds_bucket`, plus `go_*`/`process_*` dropped |
| `karpenter` | Autoscaler counters and capacity | Only `karpenter_nodeclaims_created_total`, `karpenter_nodes_created_total`, `karpenter_nodes_terminated_total`, `karpenter_nodepools_usage`, `karpenter_nodepools_limit`, `karpenter_nodes_allocatable`, `karpenter_interruption_received_messages_total` |
| `git-cache-central-metrics` | Git cache central service metrics | No filter — all `git_cache_central_*` flow through |
| Git cache daemonset | Per-node git cache metrics (PodMonitor) | No filter — all `git_cache_node_*` flow through |
| `arc-listeners` | ARC runner-scale-set listener metrics (PodMonitor) | Keeps `gha_assigned_jobs`, `gha_completed_jobs_total`, `gha_started_jobs_total`, `gha_running_jobs`, `gha_job_*_duration_seconds_(sum\|count)`, `gha_capacity_*` |
| `arc-controller` | ARC controller metrics | See ServiceMonitor for filter |
| `harbor` | Harbor registry exporter | All metrics except `go_*`/`process_*`/`promhttp_*` |
| `buildkit` / `buildkit-haproxy` | BuildKit pods + LB metrics | See ServiceMonitors for filter |
| `node-compactor` | Node consolidation/compactor metrics | No filter |
| `pushgateway` | Prometheus Pushgateway (ad-hoc job metrics) | No filter |
| `pypi-cache` | PyPI cache nginx metrics | Only `nginx_up`, `nginx_http_requests_total`, `nginx_connections_active` |
| `dcgm-exporter` | NVIDIA GPU metrics (only on GPU nodes) | See ServiceMonitor for filter |

In addition, the `kube-prometheus-stack-operator` job exists but Alloy's `cost_control` drops every `prometheus_operator_*` metric (and `go_*`/`process_*`/`promhttp_*`), so only the `up` metric is queryable in practice.

Job naming follows two conventions:

- **ServiceMonitors**: the `job` label is the target Service name (e.g. `karpenter`, `kubernetes` for the API server, `buildkitd-pods`).
- **PodMonitors**: the `job` label is `<namespace>/<podmonitor-name>` (e.g. `monitoring/arc-listeners`, `monitoring/coredns`, `monitoring/git-cache-daemonset`).

Confirm exact job names by running `count(up{cluster="..."}) by (job)`.

## Key Labels

All metrics include `cluster="pytorch-arc-cbr-production"` (set by Alloy as an external label). Other common labels:

| Label | Example | Notes |
|-------|---------|-------|
| `namespace` | `arc-runners`, `kube-system` | Kubernetes namespace |
| `pod` | `runner-xyz-abc` | Pod name |
| `container` | `runner`, `git-cache` | Container name |
| `node` | `ip-10-4-154-0.us-east-2.compute.internal` | Node hostname (use `kube_pod_info` to map pod → node) |
| `instance` | `10.4.154.0:9100` | Scrape target address |
| `job` | `node-exporter`, `karpenter` | Scrape job name |
| `nodepool` | `c7i-12xlarge`, `g5-48xlarge` | **Only present on Karpenter and node-compactor metrics**, NOT a global label |

## Example Queries

> **IMPORTANT**: After PR #428 (2026-04-08) tightened the metric allowlist, many "obvious" PromQL queries return zero results because the source metric is dropped at scrape time. The examples below only use metrics that actually pass through the three filtering layers. See `modules/monitoring/CLAUDE.md` for the layer breakdown, and the "Available Scrape Jobs" table above for what each job emits.

### Cluster Overview

```promql
# Node count
count(kube_node_info{cluster="pytorch-arc-cbr-production"})

# Pod count by namespace
count(kube_pod_info{cluster="pytorch-arc-cbr-production"}) by (namespace)

# Cluster-wide memory available (bytes)
sum(node_memory_MemAvailable_bytes{cluster="pytorch-arc-cbr-production"})

# Cluster-wide memory usage %
100 * (1 - sum(node_memory_MemAvailable_bytes{cluster="pytorch-arc-cbr-production"}) / sum(node_memory_MemTotal_bytes{cluster="pytorch-arc-cbr-production"}))

# Up targets by job
count(up{cluster="pytorch-arc-cbr-production"}) by (job)
```

> **Note**: CPU, disk, and network metrics from `node_exporter` are dropped at scrape (only `node_memory_MemAvailable_bytes` and `node_memory_MemTotal_bytes` are kept). For per-pod CPU you need a different source (e.g. KSM pod resource metrics) — `node_cpu_seconds_total`, `node_filesystem_*`, and `node_network_*` will return empty.

### Node Metrics

```promql
# Memory available per node (bytes)
node_memory_MemAvailable_bytes{cluster="pytorch-arc-cbr-production"}

# Memory usage per node (%)
100 * (1 - node_memory_MemAvailable_bytes{cluster="pytorch-arc-cbr-production"} / node_memory_MemTotal_bytes{cluster="pytorch-arc-cbr-production"})

# Running pod count per node
kubelet_running_pods{cluster="pytorch-arc-cbr-production"}

# Running container count per node
kubelet_running_containers{cluster="pytorch-arc-cbr-production"}
```

### Pod / Container Health

```promql
# Pod-to-node mapping (use to join pod-level metrics with node info)
kube_pod_info{cluster="pytorch-arc-cbr-production"}

# Container restarts in last 24h — control-plane namespaces only
# (kube_pod_container_status_restarts_total is filtered to arc-systems|karpenter|harbor-system|monitoring|logging|buildkit by Alloy)
topk(10, increase(kube_pod_container_status_restarts_total{cluster="pytorch-arc-cbr-production"}[24h]))

# Pods that exited with a non-Completed reason (last terminated)
kube_pod_container_status_last_terminated_reason{cluster="pytorch-arc-cbr-production"}

# Pods that exited with non-zero exit code
kube_pod_container_status_last_terminated_exitcode{cluster="pytorch-arc-cbr-production", container_exit_code!="0"}

# Pod status reasons (Evicted, NodeLost, UnexpectedAdmissionError — routine reasons are dropped)
kube_pod_status_reason{cluster="pytorch-arc-cbr-production"} == 1

# Container memory (working set) — control-plane namespaces only
# (Alloy drops container_memory_working_set_bytes for non-control-plane namespaces)
container_memory_working_set_bytes{cluster="pytorch-arc-cbr-production", namespace="monitoring"}
```

> **Note**: `kube_pod_status_phase` is NOT in the KSM keep allowlist — phase-based queries return nothing. Use `kube_pod_info` joined with `kube_pod_container_status_*` instead. Container CPU (`container_cpu_usage_seconds_total`) is dropped at scrape — only the two memory metrics survive cAdvisor filtering.

### Karpenter (Autoscaling)

```promql
# NodePool usage (current allocation per nodepool)
karpenter_nodepools_usage{cluster="pytorch-arc-cbr-production"}

# NodePool limits
karpenter_nodepools_limit{cluster="pytorch-arc-cbr-production"}

# Allocatable capacity per Karpenter-managed node
karpenter_nodes_allocatable{cluster="pytorch-arc-cbr-production"}

# Nodes created in the last hour
increase(karpenter_nodes_created_total{cluster="pytorch-arc-cbr-production"}[1h])

# Nodes terminated in the last hour
increase(karpenter_nodes_terminated_total{cluster="pytorch-arc-cbr-production"}[1h])

# NodeClaims created (provisioning attempts)
increase(karpenter_nodeclaims_created_total{cluster="pytorch-arc-cbr-production"}[1h])

# Spot interruptions received in the last hour
increase(karpenter_interruption_received_messages_total{cluster="pytorch-arc-cbr-production"}[1h])
```

> **Note**: Only the seven karpenter metrics above are kept by the Karpenter ServiceMonitor. `karpenter_nodepools_allowed_disruptions`, `karpenter_nodes_total`, and `karpenter_provisioner_scheduling_duration_seconds` will return empty.

### Git Cache

```promql
# Repo sizes (bytes) — central git cache
git_cache_central_repo_size_bytes{cluster="pytorch-arc-cbr-production"}

# Fetch errors in last hour — central git cache
increase(git_cache_central_fetch_errors_total{cluster="pytorch-arc-cbr-production"}[1h])

# Fetch duration — central git cache
git_cache_central_fetch_duration_seconds{cluster="pytorch-arc-cbr-production"}

# Per-node cache age (seconds since last sync)
time() - git_cache_node_last_sync_timestamp{cluster="pytorch-arc-cbr-production"}

# Per-node cache size (bytes)
git_cache_node_cache_size_bytes{cluster="pytorch-arc-cbr-production"}

# Per-node sync duration
git_cache_node_sync_duration_seconds{cluster="pytorch-arc-cbr-production"}
```

### ARC Listeners (Runner Scale Sets)

```promql
# Currently assigned jobs per runner scale set
gha_assigned_jobs{cluster="pytorch-arc-cbr-production"}

# Currently running jobs per runner scale set
gha_running_jobs{cluster="pytorch-arc-cbr-production"}

# Job completion rate (last hour)
rate(gha_completed_jobs_total{cluster="pytorch-arc-cbr-production"}[1h])

# Job start rate (last hour)
rate(gha_started_jobs_total{cluster="pytorch-arc-cbr-production"}[1h])

# Average job execution duration (sum/count — buckets dropped)
rate(gha_job_execution_duration_seconds_sum{cluster="pytorch-arc-cbr-production"}[1h])
  / rate(gha_job_execution_duration_seconds_count{cluster="pytorch-arc-cbr-production"}[1h])
```

### Control Plane

```promql
# API server request rate by verb
sum by (verb) (rate(apiserver_request_total{cluster="pytorch-arc-cbr-production"}[5m]))

# API server terminations (rate-limited / dropped requests)
rate(apiserver_request_terminations_total{cluster="pytorch-arc-cbr-production"}[5m])

# CoreDNS request rate
rate(coredns_dns_requests_total{cluster="pytorch-arc-cbr-production"}[5m])
```

### NodeLocal DNSCache (NLD)

Per-node DaemonSet — each pod emits CoreDNS plugin metrics on port `9253` (zone-scoped request/cache counters) and the binary's own `coredns_nodecache_setup_errors_total` on port `9353`. The PodMonitor labels both ports with `job="monitoring/nodelocaldns"` and `pod=<nld-pod-name>` (one per node, host-network).

> **Metric name gotcha**: the binary's setup-error counter is **`coredns_nodecache_setup_errors_total`** (namespace `coredns`, subsystem `nodecache`), **NOT** `nodelocaldns_setup_errors_total`. Some upstream/internal docs and PR drafts use the wrong name. Queries against `nodelocaldns_setup_errors_total` will silently return zero results — always use the `coredns_nodecache_*` form.

```promql
# Cluster-wide NLD QPS across all zones (one pod per node)
sum(rate(coredns_dns_request_count_total{cluster="pytorch-arc-cbr-production", job="monitoring/nodelocaldns"}[5m]))

# Per-zone NLD QPS (cluster.local.:53, in-addr.arpa.:53, ip6.arpa.:53, .:53)
sum by (server) (rate(coredns_dns_request_count_total{cluster="pytorch-arc-cbr-production", job="monitoring/nodelocaldns"}[5m]))

# Per-node NLD QPS (top 10 noisiest nodes)
topk(10, sum by (pod) (rate(coredns_dns_request_count_total{cluster="pytorch-arc-cbr-production", job="monitoring/nodelocaldns"}[5m])))

# Cluster-wide cache hit ratio (success hits / (success hits + misses))
sum(rate(coredns_cache_hits_total{cluster="pytorch-arc-cbr-production", job="monitoring/nodelocaldns", type="success"}[5m]))
  / (sum(rate(coredns_cache_hits_total{cluster="pytorch-arc-cbr-production", job="monitoring/nodelocaldns", type="success"}[5m]))
     + sum(rate(coredns_cache_misses_total{cluster="pytorch-arc-cbr-production", job="monitoring/nodelocaldns"}[5m])))

# Per-node cache hit ratio (find nodes with poor cache locality)
sum by (pod) (rate(coredns_cache_hits_total{cluster="pytorch-arc-cbr-production", job="monitoring/nodelocaldns", type="success"}[5m]))
  / (sum by (pod) (rate(coredns_cache_hits_total{cluster="pytorch-arc-cbr-production", job="monitoring/nodelocaldns", type="success"}[5m]))
     + sum by (pod) (rate(coredns_cache_misses_total{cluster="pytorch-arc-cbr-production", job="monitoring/nodelocaldns"}[5m])))

# NLD setup errors (any non-zero value should fire NodeLocalDNSSetupErrors alert)
coredns_nodecache_setup_errors_total{cluster="pytorch-arc-cbr-production"}

# Per-node NLD setup errors broken down by error type
sum by (pod, errortype) (coredns_nodecache_setup_errors_total{cluster="pytorch-arc-cbr-production"})

# Number of NLD pods reporting metrics (should equal node count)
count(up{cluster="pytorch-arc-cbr-production", job="monitoring/nodelocaldns"} == 1)
```

### PyPI Cache

```promql
# nginx availability per pypi-cache instance
nginx_up{cluster="pytorch-arc-cbr-production"}

# Request rate
rate(nginx_http_requests_total{cluster="pytorch-arc-cbr-production"}[5m])

# Active connections
nginx_connections_active{cluster="pytorch-arc-cbr-production"}
```

### Bad Node Detection (Join Pattern)

`kube_pod_info` provides pod-to-node mapping. Join with error metrics to find nodes producing failures:

```promql
count(
  kube_pod_container_status_last_terminated_exitcode{cluster="pytorch-arc-cbr-production", container_exit_code!="0"}
  * on(namespace, pod) group_left(node) kube_pod_info{cluster="pytorch-arc-cbr-production"}
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
# Find all git_cache metrics
curl -s -u "$MIMIR_USER:$MIMIR_PASS" \
    "$MIMIR_URL/api/v1/query" \
    --data-urlencode 'query={cluster="pytorch-arc-cbr-production", __name__=~"git_cache.*"}' \
    | jq '[.data.result[] | .metric.__name__] | unique'

# Find all karpenter metrics
curl -s -u "$MIMIR_USER:$MIMIR_PASS" \
    "$MIMIR_URL/api/v1/query" \
    --data-urlencode 'query={cluster="pytorch-arc-cbr-production", __name__=~"karpenter.*"}' \
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
