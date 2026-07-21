---
name: osdc-observability
description: >
  OSDC observability: monitoring (metrics) and logging pipelines, three-Alloy architecture,
  Grafana Cloud Loki + Mimir queries, label strategy, credential setup,
  and troubleshooting.
  Applies to ~/meta/ci-infra/osdc.
  Load when working on monitoring, logging, Alloy, or querying logs.
---

# OSDC Observability: Monitoring + Logging

OSDC has two observability pipelines, both pushing to Grafana Cloud. They use **three Grafana Alloy installations** to avoid RBAC and config collisions:

| Pipeline | Component | Location | Alloy Mode | Namespace | Helm Release |
|----------|-----------|----------|------------|-----------|--------------|
| **Metrics** | `modules/monitoring/` | Module (opt-in) | Deployment (2 replicas, clustered) | `monitoring` | `alloy` |
| **Logs** | `modules/logging/` | Module (opt-in) | DaemonSet (one per node) | `logging` | `alloy-logging` |
| **Events** | `modules/logging/` | Module (opt-in) | Deployment (1 replica) | `logging` | `alloy-events` |

Both modules are **secret-gated**: Alloy only installs if a `grafana-cloud-credentials` secret exists in the respective namespace. The secrets have different keys (metrics uses `username`/`password`; logging uses `loki-username`/`loki-api-key-write`/`loki-api-key-read`). All Grafana Cloud URLs come from `clusters.yaml`, not the secret.

**Pinned chart versions:**
- `kube-prometheus-stack`: `82.10.3` (in `modules/monitoring/deploy.sh`)
- `prometheus-pushgateway`: `3.6.0` (in `modules/monitoring/deploy.sh`)
- Alloy chart: `1.6.2` (from `clusters.yaml` `alloy_chart_version`, used by both modules)

## Monitoring (metrics pipeline)

The `monitoring` module uses kube-prometheus-stack as a **CRD + exporter bundle only** — Prometheus, Grafana, and AlertManager are all disabled. Alloy discovers ServiceMonitor/PodMonitor CRDs, scrapes targets, and pushes to Grafana Cloud Mimir. A separate `prometheus-pushgateway` Deployment is installed for short-lived job pushes.

What kube-prometheus-stack provides: CRDs (`monitoring.coreos.com`), Prometheus Operator, node-exporter (DaemonSet), kube-state-metrics.

**What's scraped:**

| Type | Name | Target Namespace | What it monitors |
|------|------|-----------------|-----------------|
| ServiceMonitor | apiserver | default | Kubernetes API server metrics |
| ServiceMonitor | arc-controller | arc-systems | ARC controller metrics |
| ServiceMonitor | buildkit | buildkit | BuildKit builder pod metrics |
| ServiceMonitor | buildkit-haproxy | buildkit | BuildKit HAProxy load balancer metrics |
| ServiceMonitor | dcgm-exporter | monitoring | NVIDIA GPU metrics (DCGM) — file lives directly under `monitors/dcgm-servicemonitor.yaml`, not in the `servicemonitors/` subdir |
| ServiceMonitor | harbor | harbor-system | Harbor exporter metrics |
| ServiceMonitor | karpenter | karpenter | Karpenter controller metrics |
| ServiceMonitor | keda | keda | KEDA operator metrics (`keda_*` keep-list; `*_bucket` dropped) |
| ServiceMonitor | node-compactor | kube-system | Node compactor metrics |
| ServiceMonitor | pushgateway | monitoring | Prometheus Pushgateway metrics |
| ServiceMonitor | pypi-cache | pypi-cache | PyPI cache nginx + pypiserver metrics |
| PodMonitor | arc-listeners | arc-systems | ARC listener pods metrics (interval `3m` — the only sub-60s exception; all other monitors run at 60s to halve Grafana DPM billing) |
| PodMonitor | coredns | kube-system | CoreDNS resolver metrics |
| PodMonitor | nodelocaldns | kube-system | NodeLocal DNSCache — TWO endpoints per pod: `:9253` (`coredns_*` plugin metrics from the Corefile `prometheus` directive) AND `:9353` (`coredns_nodecache_*` setup-error counters emitted by the binary itself, NOT a CoreDNS plugin). Adding only `:9253` would silently miss `coredns_nodecache_setup_errors_total` — the alert metric. |

**Currently disabled under IPv6-only EKS:** the built-in kubelet ServiceMonitor (`/metrics` + cAdvisor) is **OFF** (`kubelet.enabled: false` in `modules/monitoring/helm/values.yaml`). Prometheus Operator picks `node.status.addresses[0]` for the kubelet Endpoints object, which on dual-stack nodes is the IPv4 InternalIP — unreachable from IPv6-only Alloy pods. **Consequence:** `container_memory_working_set_bytes`, `container_memory_rss`, `kubelet_running_pods`, `kubelet_running_containers` are NOT in Mimir today. This also makes the `cost_control` rule's control-plane scoping (below) effectively a no-op for those metrics until a custom IPv6-aware kubelet ServiceMonitor is shipped. See `docs/observability.md` "Disabled scrapers (IPv6 migration)".

### Layered Metrics Filtering (Three Layers)

Cardinality control happens at three distinct layers — knowing which layer a rule lives in matters when debugging "why is metric X missing?":

1. **KSM allowlist** (`modules/monitoring/helm/values.yaml`, kube-state-metrics `extraArgs`): `--metric-allowlist=kube_(daemonset|deployment|pod|namespace|node|statefulset|persistentvolume|horizontalpodautoscaler|job)_.+` — drops entire metric families at the exporter before they're scraped. KSM's `monitor.metricRelabelings` then drops `kube_node_status_addresses`, benign `Completed` terminations (`kube_pod_container_status_(last_)?terminated_reason;Completed`), exit-code-0 terminations (`kube_pod_container_status_last_terminated_exitcode;0`), and routine status reasons (`kube_pod_status_reason;(Shutdown|NodeAffinity)`) — keeping only real errors like `Evicted`, `NodeLost`, `UnexpectedAdmissionError`.
2. **ServiceMonitor `metricRelabelings`** (`modules/monitoring/kubernetes/monitors/servicemonitors/*.yaml`): per-target keep/drop rules. Examples: apiserver keeps only `apiserver_request_total|apiserver_request_terminations_total`; karpenter keeps a focused list and drops histograms; harbor/buildkit drop `go_.*|process_.*|promhttp_.*`. CoreDNS/API server low-value metrics are dropped here. **node-exporter** uses a tight keep-allowlist: only `node_memory_MemAvailable_bytes`, `node_memory_MemTotal_bytes`, `node_nf_conntrack_entries`, `node_nf_conntrack_entries_limit` survive — so CPU/disk/network device metrics are NOT in Mimir by design. (cAdvisor relabel rules would also live here — but the kubelet ServiceMonitor is currently off under IPv6-only EKS, so no cAdvisor metrics reach Alloy.)
3. **Alloy `cost_control` relabel** (`modules/monitoring/helm/alloy-values.yaml`, `prometheus.relabel "cost_control"`): final pass before `remote_write`. Five rule groups:
   - **KSM low-value**: `kube_.*_created`, `kube_.*_metadata_resource_version`, `kube_secret_.*`, `kube_configmap_.*`, `kube_endpoint_.*`, `kube_lease_.*` — blanket drop
   - **Control-plane-only scoping**: `kube_pod_container_status_restarts_total`, `container_memory_working_set_bytes`, `container_memory_rss` — kept ONLY for `arc-systems|karpenter|harbor-system|monitoring|logging|buildkit`. Two-step replace+drop pattern (RE2 has no lookahead). Note: the two `container_memory_*` metrics are sourced from cAdvisor via the kubelet ServiceMonitor — currently disabled under IPv6-only EKS — so this scoping rule is effectively a no-op for those two metrics until a custom IPv6-aware kubelet ServiceMonitor lands
   - **Misc high-cardinality**: `kubernetes_feature_enabled` — blanket drop
   - **ARC histogram buckets**: `gha_job_(execution|startup)_duration_seconds_bucket` — dropped (sum/count kept)
   - **Runtime internals**: `go_.*`, `process_.*`, `promhttp_.*`, `prometheus_operator_.*` — blanket drop

### Alerting Architecture — No Local Prometheus

PrometheusRule CRDs in `kubernetes/alerts/` are NOT evaluated locally (no Prometheus instance). Alloy's `mimir.rules.kubernetes` syncs them to Grafana Cloud Mimir, which evaluates rules and routes alerts via Grafana Cloud Alerting.

Alert groups present:
- `arc-alerts.yaml`
- `gpu-alerts.yaml`
- `harbor-cache-recovery-alerts.yaml`
- `infrastructure-alerts.yaml`
- `network-pressure-alerts.yaml` (group `ipv6-network-pressure`: `NodeConntrackHighWarn` >80%, `NodeConntrackHighCritical` >95%, `NodeConntrackMetricMissing` — relevant under IPv6-only EKS where pod→IPv4-internet egress goes through VPC CNI's IPv4 SNAT, which is conntrack-heavy)
- `node-compactor-alerts.yaml`
- `nodelocaldns-alerts.yaml` (`NodeLocalDNSSetupErrors` critical, `NodeLocalDNSPodRestarting` warning, `NodeLocalDNSDaemonSetDegraded` warning — note the `coredns_nodecache_*` metric prefix, NOT `nodelocaldns_*`)
- `zombie-cleanup-alerts.yaml`

### DCGM GPU Metrics — Custom Subset

DCGM exporter uses a curated 23-metric subset via a ConfigMap at `modules/monitoring/kubernetes/dcgm-exporter/custom-metrics-configmap.yaml` (mounted to `/etc/dcgm-exporter/custom-metrics.csv` in the pod). Composition: Tier 1 essentials (14) + Tier 1 errors (6) + Tier 2 diagnostic (3) = 23. High-cardinality labels dropped via `metricRelabelings`: `UUID`, `modelName`, `DCGM_FI_DRIVER_VERSION`, `pci_bus_id`.

**Image pin + resource shape** (`modules/monitoring/kubernetes/dcgm-exporter/daemonset.yaml`):
- Image: `nvcr.io/nvidia/k8s/dcgm-exporter:4.5.2-4.8.1-distroless`
- Args: `-f /etc/dcgm-exporter/custom-metrics.csv --collect-interval=60000` (60s — matches the global 60s scrape interval)
- Resources: `requests=100m cpu / 256Mi memory`, `limits=200m cpu / 512Mi memory`
- `GOMEMLIMIT=410MiB` (~80% of the 512Mi cgroup limit) — keeps Go GC inside the cgroup ceiling.
- Runs only on `nvidia.com/gpu.present=true` nodes; tolerates `nvidia.com/gpu`, `instance-type`, `node-fleet` taints.

### Configuration (clusters.yaml)

```yaml
defaults:
  monitoring:
    grafana_cloud_url: "https://prometheus-prod-36-prod-us-west-0.grafana.net/api/prom/push"
    grafana_cloud_read_url: "https://prometheus-prod-36-prod-us-west-0.grafana.net/api/prom"
  alloy_chart_version: "1.6.2"
```

The `namespace` key is not in `clusters.yaml` — `deploy.sh` defaults it to `monitoring` (and `logging` for the logging module). All accessible clusters today (meta-staging/prod, arc-cbr-production*, lf-prod-aws-ue1/ue2) enable both `monitoring` and `logging` in their `modules:` list.

### Dashboards (sibling `grafana/` directory, NOT inside `osdc/`)

Dashboards no longer live under version control inside `osdc/`. They were moved to a sibling top-level `grafana/` folder at repo root and are published to Grafana Cloud by the `.github/workflows/grafana-publish.yml` workflow on every push to `main` that touches `grafana/**`.

- Tooling: `gcx` (Grafana Cloud explorer CLI) pinned via `grafana/mise.toml`.
- Generator: `grafana/generator.py` (creates `grafana/generated/` resource files from the top-level `*.json` dashboard sources, currently `osdc.json` and `pytorch_devx.json`).
- Tasks: `mise run generate --folder <UID>`, `mise run validate --folder <UID>`, `mise run push --folder <UID>`. The CI workflow runs `mise run push --folder fvnzj9` (the OSDC folder UID at `https://pytorchci.grafana.net/dashboards/f/fvnzj9`).
- Credentials: `GRAFANA_TOKEN` (CI secret `PYTORCHCI_GRAFANA_API_TOKEN`). Never persist in shell profiles, logs, or commits.
- Dashboard panel queries reference the OSDC metrics defined in this module; see `grafana/CLAUDE.md` for the runner_group_name ↔ cluster mapping table that must be kept in sync when adding new clusters.

## Logging (log collection pipeline)

`modules/logging/` collects system journal entries and Kubernetes events, pushing to Grafana Cloud Loki. **Container stdout/stderr is intentionally NOT shipped** — the runner-job log volume was too costly relative to the value (GitHub Actions stores full workflow logs already), and a long-broken Alloy file-discovery path made the absence invisible until smoke tests were hardened.

Two log sources:
- **System journal**: `loki.source.journal` from `/var/log/journal` — DaemonSet `alloy-logging`, units `kubelet.service|containerd.service|kernel|nvidia-fabricmanager.service|nvidia-persistenced.service`
- **Kubernetes events**: `loki.source.kubernetes_events` — single-replica Deployment `alloy-events` with its own inline config (JSON parsing, label promotion, structured metadata, plus its own feedback-loop drop for the `logging` namespace)

### Journal Pipeline

`modules/logging/pipelines/base.alloy` defines the journal pipeline. It maps RFC 5424 syslog priorities to levels (0-3→`error`, 4→`warn`, 5-6→`info`, 7→`debug`), **drops** priority-7 (`level=debug`) entries with `drop_counter_reason="journal_debug"`, and moves `node` to structured metadata so it stays queryable without indexing cost.

### Configuration (clusters.yaml)

```yaml
logging:
  grafana_cloud_loki_url: "https://logs-prod-021.grafana.net/loki/api/v1/push"
```

## Label Strategy

| Label | Type | Query Usage |
|-------|------|-------------|
| `cluster` | external_label | `{cluster="..."}` |
| `unit` | indexed | `{unit="..."}` (journal logs) |
| `level` | indexed | `{level="error"}` (journal, from priority mapping) |
| `node` | structured metadata (Loki 3.x) | `{...} \| node="..."` (pipe, NOT inside `{}`) |
| `type`, `kind` | indexed (events only) | `{type="Warning", kind="Pod"}` |
| `reason`, `sourcecomponent` | structured metadata (events only) | `{kind="Pod"} \| reason="..."` |

`node` is moved to structured metadata via `stage.structured_metadata` + `stage.label_drop`. Use the pipe `|` syntax to filter, NOT label matchers `{}`.

## Key Gotchas (both pipelines)

- **RBAC isolation**: logging uses `fullnameOverride: alloy-logging`, events uses `alloy-events` — avoids ClusterRole/ClusterRoleBinding collision with the monitoring Alloy
- **Clustering**: monitoring Alloy enables `clustering { enabled = true }` for HA target dedup across the 2 replicas; logging Alloy disables clustering (DaemonSet, each pod owns its node's logs)
- **CRD ordering**: ServiceMonitor/PodMonitor CRDs don't exist until kube-prometheus-stack installs. Monitors live in `kubernetes/monitors/` applied by `deploy.sh` after Helm install
- **Admission webhook**: kube-prometheus-stack's admission webhook job has no tolerations by default. On OSDC clusters where all base nodes are tainted, the job stays Pending forever. Helm values must include tolerations for `prometheusOperator.admissionWebhooks.patch` (`CriticalAddonsOnly`)
- **IPv6 dual-stack listen address**: both logging Alloy releases (`alloy-logging` DaemonSet and `alloy-events` Deployment) set `alloy.listenAddr: "[::]"` in their Helm values. The chart default `0.0.0.0` is IPv4-only and the kubelet readinessProbe on IPv6-only EKS targets the pod's IPv6 address, leaving the pod stuck NotReady. Same rationale for node-exporter's `listenOnAllInterfaces: false` (forces bind to `status.hostIP` so the IPv6 address is reachable)
- **Credential setup**: each pipeline needs its own secret created manually before first deploy (see below)

## Credential Setup (one-time per cluster)

**Monitoring namespace** (Alloy push + smoke test read) — secret holds ONLY `username`/`password` for Mimir write. The Loki keys belong in the `logging` secret, not here:
```bash
kubectl create secret generic grafana-cloud-credentials \
  -n monitoring \
  --from-literal=username='<GRAFANA_CLOUD_METRICS_USER_ID>' \
  --from-literal=password='<API_KEY_WITH_METRICS_WRITE_SCOPE>'

# Separate read secret for Mimir queries (see "Querying" below)
kubectl create secret generic grafana-cloud-read-credentials \
  -n monitoring \
  --from-literal=username='<GRAFANA_CLOUD_METRICS_USER_ID>' \
  --from-literal=password='<API_KEY_WITH_METRICS_READ_SCOPE>'
```

**Logging namespace:**
```bash
kubectl create namespace logging
kubectl create secret generic grafana-cloud-credentials \
  -n logging \
  --from-literal=loki-username='<GRAFANA_CLOUD_LOKI_USER_ID>' \
  --from-literal=loki-api-key-write='<API_KEY_WITH_LOGS_WRITE_SCOPE>' \
  --from-literal=loki-api-key-read='<API_KEY_WITH_LOGS_READ_SCOPE>'
```

Both pipelines skip silently if their secret is missing. Grafana Cloud URLs come from `clusters.yaml` (`monitoring.grafana_cloud_url` for write, `monitoring.grafana_cloud_read_url` for read), NOT from secrets.

## Querying Logs in Grafana Cloud Loki

When `kubectl logs` is unavailable (pod terminated, node evicted), historical logs survive in Grafana Cloud Loki.

**ALL `kubectl` calls require the Meta corporate proxy bypass** (`NO_PROXY="$NO_PROXY,.eks.amazonaws.com" no_proxy="$no_proxy,.eks.amazonaws.com"`) and `mise exec --` prefix. Without this, every `kubectl` call hits "connection refused".

**Step 1 — Extract credentials:**

```bash
LOKI_USER=$(NO_PROXY="$NO_PROXY,.eks.amazonaws.com" no_proxy="$no_proxy,.eks.amazonaws.com" \
  mise exec -- kubectl get secret grafana-cloud-credentials -n logging \
  -o jsonpath='{.data.loki-username}' | base64 -d)

LOKI_READ_KEY=$(NO_PROXY="$NO_PROXY,.eks.amazonaws.com" no_proxy="$no_proxy,.eks.amazonaws.com" \
  mise exec -- kubectl get secret grafana-cloud-credentials -n logging \
  -o jsonpath='{.data.loki-api-key-read}' | base64 -d)

LOKI_URL="https://logs-prod-021.grafana.net"
```

The Loki URL comes from `clusters.yaml` (`logging.grafana_cloud_loki_url`), minus the `/loki/api/v1/push` suffix.

**Step 2 — Query with curl:**

```bash
# Journal logs by unit (kubelet, containerd)
curl -s -u "$LOKI_USER:$LOKI_READ_KEY" \
    "$LOKI_URL/loki/api/v1/query_range" \
    --data-urlencode 'query={cluster="<cluster-name>", unit="kubelet.service"}' \
    --data-urlencode "limit=100" \
    --data-urlencode "start=$(date -u -v-1H +%s)000000000" \
    --data-urlencode "end=$(date -u +%s)000000000" | jq .

# Filter by node (structured metadata — pipe syntax)
curl -s -u "$LOKI_USER:$LOKI_READ_KEY" \
    "$LOKI_URL/loki/api/v1/query_range" \
    --data-urlencode 'query={cluster="<cluster-name>", unit="kubelet.service"} | node="<node-name>"' \
    --data-urlencode "limit=100" \
    --data-urlencode "start=$(date -u -v-1H +%s)000000000" \
    --data-urlencode "end=$(date -u +%s)000000000" | jq .

# Pod logs by namespace + container
curl -s -u "$LOKI_USER:$LOKI_READ_KEY" \
    "$LOKI_URL/loki/api/v1/query_range" \
    --data-urlencode 'query={cluster="<cluster-name>", namespace="arc-runners", container="runner"}' \
    --data-urlencode "limit=100" \
    --data-urlencode "start=$(date -u -v-1H +%s)000000000" \
    --data-urlencode "end=$(date -u +%s)000000000" | jq .

# Filter pod logs by specific pod (structured metadata)
curl -s -u "$LOKI_USER:$LOKI_READ_KEY" \
    "$LOKI_URL/loki/api/v1/query_range" \
    --data-urlencode 'query={cluster="<cluster-name>", namespace="arc-runners"} | pod="<pod-name>"' \
    --data-urlencode "limit=100" \
    --data-urlencode "start=$(date -u -v-6H +%s)000000000" \
    --data-urlencode "end=$(date -u +%s)000000000" | jq .

# List labels / values
curl -s -u "$LOKI_USER:$LOKI_READ_KEY" "$LOKI_URL/loki/api/v1/labels" | jq .
curl -s -u "$LOKI_USER:$LOKI_READ_KEY" "$LOKI_URL/loki/api/v1/label/cluster/values" | jq .
```

See `docs/loki_query.md` for the full reference (combined templates, time-range helpers, structured metadata reference).

## Querying Metrics in Grafana Cloud Mimir

Mimir exposes a Prometheus-compatible HTTP API. **Use the separate `grafana-cloud-read-credentials` secret in the `monitoring` namespace** — NOT `grafana-cloud-credentials` (which only has write scope for Alloy).

```bash
MIMIR_USER=$(NO_PROXY="$NO_PROXY,.eks.amazonaws.com" no_proxy="$no_proxy,.eks.amazonaws.com" \
  mise exec -- kubectl get secret grafana-cloud-read-credentials -n monitoring \
  -o jsonpath='{.data.username}' | base64 -d)

MIMIR_PASS=$(NO_PROXY="$NO_PROXY,.eks.amazonaws.com" no_proxy="$no_proxy,.eks.amazonaws.com" \
  mise exec -- kubectl get secret grafana-cloud-read-credentials -n monitoring \
  -o jsonpath='{.data.password}' | base64 -d)

MIMIR_URL="https://prometheus-prod-36-prod-us-west-0.grafana.net/api/prom"

# Instant query
curl -s -u "$MIMIR_USER:$MIMIR_PASS" \
    "$MIMIR_URL/api/v1/query" \
    --data-urlencode 'query=up{cluster="<cluster-name>"}' | jq .

# Range query
curl -s -u "$MIMIR_USER:$MIMIR_PASS" \
    "$MIMIR_URL/api/v1/query_range" \
    --data-urlencode 'query=rate(node_cpu_seconds_total{cluster="<cluster-name>", mode="idle"}[5m])' \
    --data-urlencode "start=$(date -u -v-1H +%s)" \
    --data-urlencode "end=$(date -u +%s)" \
    --data-urlencode "step=60" | jq .
```

The Mimir URL comes from `clusters.yaml` (`monitoring.grafana_cloud_read_url`). See `docs/mimir_query.md` for the full reference.

## Troubleshooting

- **Alloy not installed**: check `kubectl get secret grafana-cloud-credentials -n {logging,monitoring}` — deploy is skipped without it
- **Alloy memory**: all three Alloys have explicit Go runtime tuning via `deploy.sh` extraEnv. Centralized Deployments get generous headroom because their cost multiplies by 1 (not by node count); the DaemonSet is tuned tighter because its cost multiplies by node count.
  - **Monitoring Alloy** (Deployment, 2 replicas): `GOMEMLIMIT=7000MiB` (~85% of 8Gi cgroup), no `GOGC` override.
  - **Logging DaemonSet** (`alloy-logging`): `requests=1Gi/limits=2Gi`, `GOGC=200`, `GOMEMLIMIT=1800MiB` (~85% of 2Gi cgroup) for high-throughput CI nodes.
  - **Events Deployment** (`alloy-events`): `GOGC=200`, `GOMEMLIMIT=3500MiB` (~85% of 4Gi cgroup).
  Default upstream is 1Gi — bump only if `kubectl describe pod` shows OOMKilled. When raising the cgroup limit, also bump `GOMEMLIMIT` to ~85% of the new ceiling.
- **DCGM exporter OOM**: pod is sized `requests=256Mi/limits=512Mi` with `GOMEMLIMIT=410MiB` and 60s collection interval (`--collect-interval=60000`). Aggressive limits were chosen because the per-GPU metric set was trimmed to 23 metrics; if DCGM is OOMKilled after re-introducing metrics, bump `resources.limits.memory` AND `GOMEMLIMIT` together (keep `GOMEMLIMIT` at ~80% of the new cgroup ceiling).
- **Alloy did not pick up secret rotation**: monitoring `deploy.sh` runs `kubectl rollout restart deployment/alloy` after every successful Helm upgrade and waits on `rollout status`. Secret values are referenced via `valueFrom.secretKeyRef`, so Kubernetes will NOT restart pods when the secret value changes — only the explicit rollout does. If you rotate credentials without redeploying, force a rollout manually.
- **Missing pod logs**: container stdout/stderr is intentionally NOT shipped. Look at GitHub Actions workflow logs (for runner jobs), `kubectl logs` (while the pod is still alive), or pod events via the `alloy-events` Deployment in Loki (`{cluster="X", kind="Pod"}`)
- **Sampling-related "missing" logs**: not applicable — namespace-based sampling stages were removed with the pod-log pipeline
- **Rate limiting**: not applicable — the per-pod rate-limit stage was removed with the pod-log pipeline. Journal logs go through `loki.process "system_logs"` which has no rate limit; volume is bounded by the per-unit `keep` filter on the journal source (`kubelet|containerd|kernel|nvidia-*`)
- **Journal path empty**: EKS AL2023 uses `/var/log/journal` — hostPath uses `DirectoryOrCreate` so it won't crash, but no journal logs will appear if path is wrong
- **Dashboard publish failed**: `grafana-publish.yml` runs `mise run push --folder fvnzj9` from the `grafana/` directory. Validation is gated by `gcx resources validate`; if it fails, the dashboard JSON in `grafana/*.json` is malformed or references a missing datasource. Reproduce locally with `cd grafana && mise run validate --folder fvnzj9`.
