# Querying Grafana Cloud Loki Logs

## Context

The OSDC clusters ship three log sources to Grafana Cloud Loki: **pod logs** and **system journal logs** via an Alloy DaemonSet, and **Kubernetes events** via a separate Alloy Deployment (`alloy-events`). This is useful when `kubectl logs` is unavailable (pod terminated, node evicted, etc.) or when you need cluster-wide event history. This doc covers how to query Loki from the CLI as an agent.

## Prerequisites

- `kubectl` access to the target cluster (managed by mise in the `osdc/` directory)
- `jq` for parsing responses
- The `grafana-cloud-credentials` secret must exist in the `logging` namespace

## Step 1 — Extract Credentials

Every query session starts by pulling credentials from the cluster. These cannot be cached across Bash tool calls, so **include them at the top of every curl command block**.

```bash
LOKI_USER=$(NO_PROXY="$NO_PROXY,.eks.amazonaws.com" no_proxy="$no_proxy,.eks.amazonaws.com" \
  mise exec -- kubectl get secret grafana-cloud-credentials -n logging \
  -o jsonpath='{.data.loki-username}' | base64 -d)

LOKI_READ_KEY=$(NO_PROXY="$NO_PROXY,.eks.amazonaws.com" no_proxy="$no_proxy,.eks.amazonaws.com" \
  mise exec -- kubectl get secret grafana-cloud-credentials -n logging \
  -o jsonpath='{.data.loki-api-key-read}' | base64 -d)

LOKI_URL="https://logs-prod-021.grafana.net"
```

**Important**: The `NO_PROXY` / `no_proxy` bypass is required because of Meta's corporate proxy. Without it, `kubectl` calls to EKS will fail.

The Loki URL comes from `clusters.yaml` under `logging.grafana_cloud_loki_url`, with the `/loki/api/v1/push` suffix stripped. For `pytorch-arc-cbr-production`, the URL is `https://logs-prod-021.grafana.net`.

## Step 2 — Query

All queries use the Loki HTTP API via `curl`. The base endpoint is `$LOKI_URL/loki/api/v1/query_range`.

### Combined Template (copy-paste ready)

This is the pattern to use for every query. Replace the `query=` value with your LogQL expression:

```bash
LOKI_USER=$(NO_PROXY="$NO_PROXY,.eks.amazonaws.com" no_proxy="$no_proxy,.eks.amazonaws.com" \
  mise exec -- kubectl get secret grafana-cloud-credentials -n logging \
  -o jsonpath='{.data.loki-username}' | base64 -d) && \
LOKI_READ_KEY=$(NO_PROXY="$NO_PROXY,.eks.amazonaws.com" no_proxy="$no_proxy,.eks.amazonaws.com" \
  mise exec -- kubectl get secret grafana-cloud-credentials -n logging \
  -o jsonpath='{.data.loki-api-key-read}' | base64 -d) && \
LOKI_URL="https://logs-prod-021.grafana.net" && \
curl -s -u "$LOKI_USER:$LOKI_READ_KEY" \
    "$LOKI_URL/loki/api/v1/query_range" \
    --data-urlencode 'query={cluster="pytorch-arc-cbr-production", unit="kubelet.service"}' \
    --data-urlencode "limit=20" \
    --data-urlencode "start=$(date -u -v-1H +%s)000000000" \
    --data-urlencode "end=$(date -u +%s)000000000" | jq .
```

### Time Range Parameters

The `start` and `end` parameters are Unix timestamps in **nanoseconds**.

| Lookback | macOS `date` expression |
|----------|------------------------|
| 1 hour | `$(date -u -v-1H +%s)000000000` |
| 6 hours | `$(date -u -v-6H +%s)000000000` |
| 24 hours | `$(date -u -v-24H +%s)000000000` |
| 7 days | `$(date -u -v-7d +%s)000000000` |
| Now (end) | `$(date -u +%s)000000000` |

## Available Labels

Three log sources flow into the same Loki tenant: **pod logs**, **system journal logs**, and **Kubernetes events**. Different label sets apply per source.

### Indexed Labels (go inside `{}`)

| Label | Source | Example values | Description |
|-------|--------|---------------|-------------|
| `cluster` | all | `pytorch-arc-cbr-production` | Cluster name from clusters.yaml (external label on the writer) |
| `namespace` | pod logs | `arc-runners`, `kube-system`, `harbor-system` | Kubernetes namespace |
| `container` | pod logs | `runner`, `coredns`, `kube-proxy` | Container name |
| `app` | pod logs | `karpenter`, `harbor`, `pypi-cache` | From `app.kubernetes.io/name` pod label only — pods labeled only with `k8s-app` (EKS-managed CoreDNS, kube-proxy, NodeLocal DNS) do NOT have this label; filter by `container=` instead |
| `level` | pod + journal logs | `error`, `warn`, `info`, `fatal` | Normalized log level (lowercase). `debug` lines are dropped at ingestion (DEBUG/TRACE text-drop in pod pipeline, priority-7 drop in journal pipeline) so `level="debug"` returns few or no results |
| `unit` | journal logs | `kubelet.service`, `containerd.service`, `kernel`, `nvidia-fabricmanager.service`, `nvidia-persistenced.service` | Systemd unit |
| `kind` | k8s events | `Pod`, `Node`, `Deployment` | Event involved-object kind |
| `type` | k8s events | `Normal`, `Warning` | Event type |
| `namespace` | k8s events | `kube-system`, `arc-runners`, `harbor-system` | Namespace of the involved object (set automatically by `loki.source.kubernetes_events`) |

### Structured Metadata (go after `{}` with pipe `|` syntax)

| Label | Source | Example | Description |
|-------|--------|---------|-------------|
| `node` | pod + journal logs | `ip-10-4-154-0.us-east-2.compute.internal` | Node hostname |
| `pod` | pod logs | `runner-abcdef-xyz` | Pod name |
| `workflow_run_id` | pod logs (arc-runners) | `12345678901` | GitHub Actions workflow run id (extracted from runner pod logs) |
| `reason` | k8s events | `FailedScheduling`, `BackOff` | Event reason |
| `sourcecomponent` | k8s events | `kubelet`, `default-scheduler` | Component that emitted the event |

**Key distinction**: Structured metadata uses pipe syntax AFTER the stream selector:
```
{cluster="pytorch-arc-cbr-production", unit="kubelet.service"} | node="ip-10-4-154-0.us-east-2.compute.internal"
```

## Example Queries

### List available labels

```bash
curl -s -u "$LOKI_USER:$LOKI_READ_KEY" "$LOKI_URL/loki/api/v1/labels" | jq .
```

### List values for a label

```bash
curl -s -u "$LOKI_USER:$LOKI_READ_KEY" "$LOKI_URL/loki/api/v1/label/unit/values" | jq .
```

### Kubelet logs (last hour)

```bash
# ... (credential extraction) ... && \
curl -s -u "$LOKI_USER:$LOKI_READ_KEY" \
    "$LOKI_URL/loki/api/v1/query_range" \
    --data-urlencode 'query={cluster="pytorch-arc-cbr-production", unit="kubelet.service"}' \
    --data-urlencode "limit=20" \
    --data-urlencode "start=$(date -u -v-1H +%s)000000000" \
    --data-urlencode "end=$(date -u +%s)000000000" | jq .
```

### Kubelet logs filtered by node

```bash
# ... (credential extraction) ... && \
curl -s -u "$LOKI_USER:$LOKI_READ_KEY" \
    "$LOKI_URL/loki/api/v1/query_range" \
    --data-urlencode 'query={cluster="pytorch-arc-cbr-production", unit="kubelet.service"} | node="ip-10-4-154-0.us-east-2.compute.internal"' \
    --data-urlencode "limit=50" \
    --data-urlencode "start=$(date -u -v-6H +%s)000000000" \
    --data-urlencode "end=$(date -u +%s)000000000" | jq .
```

### Containerd logs

```bash
# ... (credential extraction) ... && \
curl -s -u "$LOKI_USER:$LOKI_READ_KEY" \
    "$LOKI_URL/loki/api/v1/query_range" \
    --data-urlencode 'query={cluster="pytorch-arc-cbr-production", unit="containerd.service"}' \
    --data-urlencode "limit=20" \
    --data-urlencode "start=$(date -u -v-1H +%s)000000000" \
    --data-urlencode "end=$(date -u +%s)000000000" | jq .
```

### Pod logs by namespace and container

```bash
# ... (credential extraction) ... && \
curl -s -u "$LOKI_USER:$LOKI_READ_KEY" \
    "$LOKI_URL/loki/api/v1/query_range" \
    --data-urlencode 'query={cluster="pytorch-arc-cbr-production", namespace="arc-runners", container="runner"}' \
    --data-urlencode "limit=50" \
    --data-urlencode "start=$(date -u -v-1H +%s)000000000" \
    --data-urlencode "end=$(date -u +%s)000000000" | jq .
```

### Filter pod logs by level

```bash
# ... (credential extraction) ... && \
curl -s -u "$LOKI_USER:$LOKI_READ_KEY" \
    "$LOKI_URL/loki/api/v1/query_range" \
    --data-urlencode 'query={cluster="pytorch-arc-cbr-production", namespace="karpenter", level="error"}' \
    --data-urlencode "limit=50" \
    --data-urlencode "start=$(date -u -v-6H +%s)000000000" \
    --data-urlencode "end=$(date -u +%s)000000000" | jq .
```

### ARC runner logs for a specific GitHub workflow run

```bash
# ... (credential extraction) ... && \
curl -s -u "$LOKI_USER:$LOKI_READ_KEY" \
    "$LOKI_URL/loki/api/v1/query_range" \
    --data-urlencode 'query={cluster="pytorch-arc-cbr-production", namespace="arc-runners"} | workflow_run_id="12345678901"' \
    --data-urlencode "limit=200" \
    --data-urlencode "start=$(date -u -v-6H +%s)000000000" \
    --data-urlencode "end=$(date -u +%s)000000000" | jq .
```

### NodeLocal DNSCache logs

NodeLocal DNSCache pods are labeled `k8s-app: node-local-dns` only (no `app.kubernetes.io/name`), so filter by container name instead:

```bash
# ... (credential extraction) ... && \
curl -s -u "$LOKI_USER:$LOKI_READ_KEY" \
    "$LOKI_URL/loki/api/v1/query_range" \
    --data-urlencode 'query={cluster="pytorch-arc-cbr-production", namespace="kube-system", container="node-cache"}' \
    --data-urlencode "limit=50" \
    --data-urlencode "start=$(date -u -v-1H +%s)000000000" \
    --data-urlencode "end=$(date -u +%s)000000000" | jq .
```

### Kubernetes events

```bash
# All events
# ... (credential extraction) ... && \
curl -s -u "$LOKI_USER:$LOKI_READ_KEY" \
    "$LOKI_URL/loki/api/v1/query_range" \
    --data-urlencode 'query={cluster="pytorch-arc-cbr-production", kind=~".+"}' \
    --data-urlencode "limit=100" \
    --data-urlencode "start=$(date -u -v-1H +%s)000000000" \
    --data-urlencode "end=$(date -u +%s)000000000" | jq .

# Warning events for Pods only
--data-urlencode 'query={cluster="pytorch-arc-cbr-production", kind="Pod", type="Warning"}'

# Filter by reason (structured metadata)
--data-urlencode 'query={cluster="pytorch-arc-cbr-production", kind=~".+"} | reason="FailedScheduling"'
```

### GPU-related journal logs

```bash
# nvidia-fabricmanager and nvidia-persistenced events on GPU nodes
--data-urlencode 'query={cluster="pytorch-arc-cbr-production", unit=~"nvidia-.+"}'

# Kernel messages (OOM kills, hardware errors)
--data-urlencode 'query={cluster="pytorch-arc-cbr-production", unit="kernel"}'
```

### Text search within logs

Use LogQL's line filter after the stream selector:

```bash
# Logs containing "error" (case-insensitive)
--data-urlencode 'query={cluster="pytorch-arc-cbr-production", unit="kubelet.service"} |~ "(?i)error"'

# Logs containing exact string "OOMKilled"
--data-urlencode 'query={cluster="pytorch-arc-cbr-production", unit="kubelet.service"} |= "OOMKilled"'

# Logs NOT containing "info"
--data-urlencode 'query={cluster="pytorch-arc-cbr-production", unit="kubelet.service"} != "info"'
```

### Compact output (just log lines)

To extract just the log text without metadata:

```bash
... | jq -r '.data.result[].values[][1]'
```

To include timestamps:

```bash
... | jq -r '.data.result[].values[] | "\(.[0]) \(.[1])"'
```

## Notes on what's collected

- **Pod logs** are collected from every node by the Alloy DaemonSet (`loki.source.file` reading CRI logs from `/var/log/pods/`). Pods in the `logging` namespace are excluded to prevent feedback loops; pods in `Succeeded` or `Failed` phase are dropped at discovery; lines matching `DEBUG`/`TRACE` levels and lines longer than 16KB are dropped; lines containing `kube-probe/` are dropped (HTTP readiness/liveness probe noise from any pod with HTTP access logging); per-pod rate limit is 1000 lines/s sustained / 5000 burst.
- **Journal logs** are collected for `kubelet.service`, `containerd.service`, `kernel`, `nvidia-fabricmanager.service`, and `nvidia-persistenced.service` only. Debug-priority entries (syslog priority 7) are dropped.
- **Kubernetes events** are collected by a separate Alloy Deployment (`alloy-events`, single replica) via the K8s Events API. Events from the `logging` namespace are dropped.
- Some module pipelines apply additional sampling (e.g. `arc-runners` non-error logs are sampled at 10%, `buildkit` non-error logs at 50%, `harbor-system` non-error logs are throttled to 100 lines/s burst 500).

## Source

Reference: `osdc-observability` skill ("Querying Logs in Grafana Cloud Loki" section) and `docs/observability.md`. Pipeline definitions in `modules/logging/pipelines/base.alloy`, `modules/logging/helm/alloy-events-values.yaml`, and per-module `modules/<name>/logging/pipeline.alloy` files.
