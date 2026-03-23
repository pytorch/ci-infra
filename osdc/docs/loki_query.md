# Querying Grafana Cloud Loki Logs

## Context

The OSDC clusters ship logs to Grafana Cloud Loki via an Alloy DaemonSet. This is useful when `kubectl logs` is unavailable (pod terminated, node evicted, etc.). This doc covers how to query Loki from the CLI as an agent.

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

### Indexed Labels (go inside `{}`)

| Label | Example values | Description |
|-------|---------------|-------------|
| `cluster` | `pytorch-arc-cbr-production` | Cluster name from clusters.yaml |
| `job` | `loki.source.journal.system` | Log source type |
| `service_name` | `loki.source.journal.system` | Service name |
| `unit` | `kubelet.service`, `containerd.service` | Systemd unit (journal logs only) |

### Structured Metadata (go after `{}` with pipe `|` syntax)

| Label | Example | Description |
|-------|---------|-------------|
| `node` | `ip-10-4-154-0.us-east-2.compute.internal` | Node hostname |
| `pod` | `runner-abcdef-xyz` | Pod name |
| `namespace` | `arc-runners` | Kubernetes namespace |
| `container` | `runner` | Container name |

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

## Current Limitations (as of 2026-03-20)

- **Only journal/system logs are ingested.** The Alloy config currently ships `kubelet.service` and `containerd.service` journal logs. Kubernetes pod logs (namespace, container, pod labels) are **not** being collected. If you need pod logs, use `kubectl logs` for live pods.
- **Structured metadata labels** (`node`, `pod`, `namespace`, `container`) may not all be populated depending on what log sources are active.

## Source

Upstream reference: `upstream/osdc/CLAUDE.md`, section "Querying Logs in Grafana Cloud Loki". Verified via live queries against `pytorch-arc-cbr-production` on 2026-03-20.
