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
    --data-urlencode 'query=rate(node_cpu_seconds_total{cluster="pytorch-arc-cbr-production", mode="idle"}[5m])' \
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

These are the `job` label values present in the cluster:

| Job | Description |
|-----|-------------|
| `node-exporter` | Node-level system metrics (CPU, memory, disk, network) — 32 targets |
| `kubelet` | Kubelet metrics (pod lifecycle, volumes, runtime) — 73 targets |
| `kube-proxy` | kube-proxy networking metrics — 29 targets |
| `kube-state-metrics` | Kubernetes object state (pods, deployments, nodes) — 1 target |
| `kube-prometheus-stack-operator` | Prometheus operator metrics — 1 target |
| `apiserver` | Kubernetes API server metrics — 2 targets |
| `coredns` | DNS resolution metrics — 2 targets |
| `karpenter` | Karpenter autoscaler metrics (NodePools, provisioning) — 2 targets |
| `monitoring/git-cache-daemonset` | Git cache per-node metrics — 24 targets |
| `git-cache-central-metrics` | Git cache central service metrics — 1 target |

## Key Labels

All metrics include `cluster="pytorch-arc-cbr-production"`. Other common labels:

| Label | Example | Notes |
|-------|---------|-------|
| `namespace` | `arc-runners`, `kube-system` | Kubernetes namespace |
| `pod` | `runner-xyz-abc` | Pod name |
| `container` | `runner`, `git-cache` | Container name |
| `node` | `ip-10-4-154-0.us-east-2.compute.internal` | Node hostname |
| `instance` | `10.4.154.0:9100` | Scrape target address |
| `job` | `node-exporter` | Scrape job name |
| `nodepool` | `c7i-12xlarge`, `g5-48xlarge` | Karpenter NodePool name |

## Example Queries

### Cluster Overview

```promql
# Node count
count(kube_node_info{cluster="pytorch-arc-cbr-production"})

# Pod count by namespace
count(kube_pod_info{cluster="pytorch-arc-cbr-production"}) by (namespace)

# Cluster-wide CPU usage %
100 - avg(rate(node_cpu_seconds_total{cluster="pytorch-arc-cbr-production", mode="idle"}[5m])) * 100

# Cluster-wide memory usage %
100 * (1 - sum(node_memory_MemAvailable_bytes{cluster="pytorch-arc-cbr-production"}) / sum(node_memory_MemTotal_bytes{cluster="pytorch-arc-cbr-production"}))

# Up targets by job
count(up{cluster="pytorch-arc-cbr-production"}) by (job)
```

### Node Metrics

```promql
# CPU usage per node (%)
100 - avg(rate(node_cpu_seconds_total{cluster="pytorch-arc-cbr-production", mode="idle"}[5m])) by (instance) * 100

# Memory usage per node (%)
100 * (1 - node_memory_MemAvailable_bytes{cluster="pytorch-arc-cbr-production"} / node_memory_MemTotal_bytes{cluster="pytorch-arc-cbr-production"})

# Disk usage per node (%)
100 * (1 - node_filesystem_avail_bytes{cluster="pytorch-arc-cbr-production", mountpoint="/"} / node_filesystem_size_bytes{cluster="pytorch-arc-cbr-production", mountpoint="/"})

# Network receive rate per node (bytes/sec)
rate(node_network_receive_bytes_total{cluster="pytorch-arc-cbr-production", device="eth0"}[5m])
```

### Pod / Container Health

```promql
# Container restarts in last 24h (top 10)
topk(10, increase(kube_pod_container_status_restarts_total{cluster="pytorch-arc-cbr-production"}[24h]))

# Pods not in Running state
kube_pod_status_phase{cluster="pytorch-arc-cbr-production", phase!="Running", phase!="Succeeded"} == 1

# Container CPU usage
rate(container_cpu_usage_seconds_total{cluster="pytorch-arc-cbr-production", namespace="arc-runners"}[5m])

# Container memory usage
container_memory_working_set_bytes{cluster="pytorch-arc-cbr-production", namespace="arc-runners"}
```

### Karpenter (Autoscaling)

```promql
# Allowed disruptions per NodePool
karpenter_nodepools_allowed_disruptions{cluster="pytorch-arc-cbr-production"}

# Nodes managed by Karpenter per NodePool
karpenter_nodes_total{cluster="pytorch-arc-cbr-production"}

# Provisioning duration
karpenter_provisioner_scheduling_duration_seconds{cluster="pytorch-arc-cbr-production"}
```

### Git Cache

```promql
# Repo sizes (bytes)
git_cache_central_repo_size_bytes{cluster="pytorch-arc-cbr-production"}

# Fetch errors in last hour
increase(git_cache_central_fetch_errors_total{cluster="pytorch-arc-cbr-production"}[1h])

# Per-node cache age (seconds since last sync)
time() - git_cache_node_last_sync_timestamp{cluster="pytorch-arc-cbr-production"}

# Per-node cache size (bytes)
git_cache_node_cache_size_bytes{cluster="pytorch-arc-cbr-production"}

# Sync duration
git_cache_node_sync_duration_seconds{cluster="pytorch-arc-cbr-production"}
```

### ARC Runners

```promql
# Runner pods by phase
count(kube_pod_status_phase{cluster="pytorch-arc-cbr-production", namespace="arc-runners"} == 1) by (phase)

# Runner container CPU usage
sum(rate(container_cpu_usage_seconds_total{cluster="pytorch-arc-cbr-production", namespace="arc-runners", container="runner"}[5m]))
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

Upstream references:
- `upstream/osdc/CLAUDE.md` — general architecture
- `upstream/osdc/modules/monitoring/CLAUDE.md` — monitoring module docs, credential setup
- `clusters.yaml` — Mimir read/write URLs
- `upstream/osdc/modules/monitoring/helm/alloy-values.yaml` — Alloy remote_write config

Verified via live queries against `pytorch-arc-cbr-production` on 2026-03-20.
