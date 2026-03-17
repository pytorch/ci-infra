# Observability: Monitoring + Logging

OSDC has two observability pipelines, both pushing telemetry to Grafana Cloud. They use **two separate Grafana Alloy installations** with distinct modes, namespaces, and RBAC to avoid collisions.

## Architecture Overview

```
                        ┌─────────────────────────────────────┐
                        │         Grafana Cloud               │
                        │  ┌──────────┐  ┌──────────────┐    │
                        │  │  Mimir   │  │    Loki      │    │
                        │  │ (metrics)│  │   (logs)     │    │
                        │  └────▲─────┘  └──────▲───────┘    │
                        └───────┼───────────────┼────────────┘
                                │               │
                   prometheus.  │               │  loki.write
                   remote_write │               │
                        ┌───────┴──────┐ ┌──────┴────────┐
                        │ Alloy        │ │ Alloy         │
                        │ (Deployment) │ │ (DaemonSet)   │
                        │ 2 replicas   │ │ 1 per node    │
                        │ ns:monitoring│ │ ns:logging    │
                        │ clustered    │ │ independent   │
                        └───────┬──────┘ └──────┬────────┘
                                │               │
                   ServiceMonitor/   loki.source.file +
                   PodMonitor CRDs   loki.source.journal
                        │               │
                  ┌─────┴─────────┐  ┌──┴──────────────┐
                  │ kube-prom-stack│  │ /var/log/pods/  │
                  │ exporters:    │  │ /var/log/journal │
                  │ - node-export │  │ (every node)    │
                  │ - kube-state  │  └─────────────────┘
                  │ - operator    │
                  └───────────────┘
```

## The Two Alloy Installations

| Aspect | Monitoring Alloy | Logging Alloy |
|--------|-----------------|---------------|
| **Location** | `modules/monitoring/` (opt-in module) | `base/logging/` (every cluster) |
| **Controller type** | Deployment (2 replicas) | DaemonSet (1 per node) |
| **Namespace** | `monitoring` | `logging` |
| **Helm release** | `alloy` | `alloy-logging` |
| **fullnameOverride** | (default) | `alloy-logging` |
| **Clustering** | Enabled (HA target dedup) | Disabled (each pod handles its node) |
| **Config source** | Helm-generated (alloy-values.yaml) | Assembled ConfigMap (`assemble_config.py`) |
| **Data destination** | Grafana Cloud Mimir (metrics) | Grafana Cloud Loki (logs) |
| **Secret name** | `grafana-cloud-credentials` in `monitoring` | `grafana-cloud-credentials` in `logging` |
| **Secret keys** | `username`, `password` (URL from `clusters.yaml`) | `loki-username`, `loki-api-key-write`, `loki-api-key-read` |

### Why two separate installations?

1. **Different controller types** — metrics scraping works with a clustered Deployment (Alloy's built-in clustering distributes scrape targets). Log collection requires a DaemonSet (each node's logs are local files).
2. **RBAC isolation** — each Alloy needs ClusterRole/ClusterRoleBinding for Kubernetes API access. Without `fullnameOverride`, Helm creates identically-named RBAC resources that collide.
3. **Independent lifecycle** — metrics and logs can be enabled/disabled separately. Logging is base infrastructure (always deployed). Monitoring is a module (opt-in).
4. **Config complexity** — monitoring Alloy config is driven by CRD discovery (ServiceMonitor/PodMonitor). Logging Alloy config is assembled from base + per-module pipeline files. Mixing these in one config would be fragile.

## Monitoring Pipeline (Metrics)

### What kube-prometheus-stack provides

The chart is used **only as a CRD + exporter bundle**:

- **CRDs**: `monitoring.coreos.com` — ServiceMonitor, PodMonitor, PrometheusRule, etc.
- **Prometheus Operator**: Manages CRD lifecycle
- **node-exporter**: DaemonSet on every node (tolerates ALL taints)
- **kube-state-metrics**: Kubernetes object state metrics (runs on base nodes)

**Prometheus, Grafana, and AlertManager are all `enabled: false`.** No in-cluster metric storage or dashboards. All metrics go to Grafana Cloud.

### What's scraped

| Type | Name | Target Namespace | What it monitors |
|------|------|-----------------|-----------------|
| ServiceMonitor | arc-controller | arc-systems | ARC controller metrics |
| ServiceMonitor | harbor | harbor-system | Harbor exporter metrics |
| ServiceMonitor | karpenter | karpenter | Karpenter controller metrics |
| ServiceMonitor | node-compactor | kube-system | Node compactor metrics |
| ServiceMonitor | git-cache-central | kube-system | Git cache central pod metrics |
| ServiceMonitor | dcgm-exporter | monitoring | NVIDIA GPU metrics (DCGM) |
| PodMonitor | git-cache-daemonset | kube-system | Git cache DaemonSet metrics |
| PodMonitor | arc-listeners | arc-runners | ARC listener pods metrics |

### Adding a new ServiceMonitor/PodMonitor

1. Create the manifest in `modules/monitoring/kubernetes/monitors/servicemonitors/` or `podmonitors/`
2. Add it to `modules/monitoring/kubernetes/monitors/kustomization.yaml`
3. Redeploy: `just deploy-module <cluster> monitoring`

Monitors are applied by `deploy.sh` after kube-prometheus-stack Helm install (CRDs must exist first).

### Credential setup (monitoring)

The Grafana Cloud URL comes from `clusters.yaml` (`monitoring.grafana_cloud_url`), not from the secret. The secret only contains authentication credentials:

```bash
kubectl create namespace monitoring
kubectl create secret generic grafana-cloud-credentials \
  -n monitoring \
  --from-literal=username='<GRAFANA_CLOUD_METRICS_USER_ID>' \
  --from-literal=password='<API_KEY_WITH_METRICS_WRITE_SCOPE>'
```

### Configuration (clusters.yaml)

```yaml
defaults:
  monitoring:
    namespace: monitoring
    grafana_cloud_url: "https://prometheus-prod-36-prod-us-west-0.grafana.net/api/prom/push"
```

## Logging Pipeline (Logs)

### Log sources

1. **Pod logs** — `loki.source.file` reads CRI-format logs from `/var/log/pods/` on each node
2. **System journal** — `loki.source.journal` reads kubelet, containerd, and kernel logs from `/var/log/journal`

Kubernetes events are NOT collected (no leader-election support in DaemonSet mode — every pod would independently duplicate events).

### Label strategy

| Label | Type | Cardinality | Purpose |
|-------|------|-------------|---------|
| `cluster` | external_label | low (~2) | Cluster-scoped queries |
| `namespace` | indexed | low (~10) | Service scoping |
| `container` | indexed | medium (~50) | Container identification |
| `app` | indexed | medium (~20) | From `app.kubernetes.io/name` pod label |
| `pod` | structured metadata | high | Queryable but not indexed (Loki 3.x) |
| `node` | structured metadata | high | Queryable but not indexed (Loki 3.x) |

**Structured metadata** (Loki 3.x) keeps high-cardinality labels queryable without indexing cost. Pod and node names change frequently — indexing them would explode Loki's index size. They're moved to structured metadata and dropped from indexed labels in the base pipeline (after `// MODULE_PIPELINES` so module `stage.match` blocks can still select on them).

### Module pipeline discovery

Each module can contribute log parsing rules by placing a `logging/pipeline.alloy` file in its module directory. The file must contain only `stage.match` blocks — syntactically valid inside a `loki.process` block.

During deploy, `assemble_config.py`:

1. Reads `clusters.yaml` to get the cluster's enabled modules
2. For each module, checks `$OSDC_ROOT/modules/<name>/logging/pipeline.alloy` (consumer override first)
3. Falls back to `$OSDC_UPSTREAM/modules/<name>/logging/pipeline.alloy`
4. Inserts all discovered blocks at the `// MODULE_PIPELINES` marker in `base.alloy`
5. Outputs the assembled config as a ConfigMap YAML (`alloy-logging-config`)

**Consumer opt-out**: an empty (whitespace-only) `pipeline.alloy` in the consumer's modules directory suppresses the upstream pipeline for that module entirely. The upstream file is NOT checked as fallback. This lets consumers disable a module's log parsing without deleting the upstream file.

**Example module pipeline** (`modules/karpenter/logging/pipeline.alloy`):

```alloy
// Karpenter (zap console encoding)
stage.match {
    selector = "{namespace=\"karpenter\"}"
    stage.regex {
        expression = "^(?P<timestamp>\\S+)\\s+(?P<level>\\S+)\\s+(?P<logger>\\S+)\\s+(?P<msg>.+)"
    }
    stage.labels { values = { level = "" } }
}
```

### Credential setup (logging)

```bash
kubectl create namespace logging
kubectl create secret generic grafana-cloud-credentials \
  -n logging \
  --from-literal=loki-username='<GRAFANA_CLOUD_LOKI_USER_ID>' \
  --from-literal=loki-api-key-write='<API_KEY_WITH_LOGS_WRITE_SCOPE>' \
  --from-literal=loki-api-key-read='<API_KEY_WITH_LOGS_READ_SCOPE>'
```

### Configuration (clusters.yaml)

```yaml
defaults:
  logging:
    namespace: logging
    grafana_cloud_loki_url: "https://logs-prod-021.grafana.net/loki/api/v1/push"
```

## Deploy Order

### Monitoring (module — runs during `deploy-module`)

1. Justfile applies `kubernetes/kustomization.yaml` (namespace + DCGM DaemonSet)
2. `deploy.sh` installs kube-prometheus-stack (CRDs + exporters)
3. `deploy.sh` applies `kubernetes/monitors/` (ServiceMonitors + PodMonitors — requires CRDs from step 2)
4. `deploy.sh` conditionally installs Alloy (if `grafana-cloud-credentials` secret exists)

### Logging (base — runs during `deploy-base`)

Within the base deploy sequence: Terraform → Mirror images → Base k8s → Harbor → **Logging** → Node Compactor (matches justfile order)

1. Creates `logging` namespace
2. Gates on `grafana-cloud-credentials` secret (exits cleanly if missing)
3. Runs `assemble_config.py` to build the ConfigMap from base + module pipelines
4. `kubectl apply` the ConfigMap
5. `helm upgrade --install alloy-logging` with runtime env vars (cluster name, Loki URL, credentials)

## Gotchas

### RBAC collision

Both Alloy installations create ClusterRole/ClusterRoleBinding resources. Without `fullnameOverride: alloy-logging` in the logging Helm values, both releases would create resources named `alloy`, causing Helm to error or silently overwrite the other's permissions.

### CRD ordering

ServiceMonitor/PodMonitor CRDs don't exist until kube-prometheus-stack installs. The monitoring module's `deploy.sh` applies monitors in a separate step after Helm install. Don't put ServiceMonitor/PodMonitor manifests in the main `kubernetes/kustomization.yaml` — they'll fail before CRDs exist.

### Admission webhook

kube-prometheus-stack's Prometheus Operator admission webhook pre-install job has no tolerations by default. On OSDC clusters where ALL base nodes are tainted with `CriticalAddonsOnly`, the job stays Pending forever. The Helm values include tolerations for `prometheusOperator.admissionWebhooks.patch`. If the job gets stuck, delete the failed Helm release and the stuck job before retrying.

### Alloy memory on high-throughput nodes

CI nodes can have 100+ concurrent runner pods each producing logs. The logging Alloy has a 1Gi memory limit by default. Under sustained backpressure (Loki down, rate-limited), Alloy may OOM. Check `kubectl describe pod` for OOMKilled events. The `stage.limit` rate limiter (10MB/s per Alloy pod) helps bound memory usage.

### Module pipeline ordering

The `// MODULE_PIPELINES` marker in `base.alloy` is placed **before** `stage.structured_metadata` and `stage.label_drop`. This means module `stage.match` blocks can select on `{pod=~"..."}` and `{node=~"..."}` labels. After the module pipelines run, pod and node are moved to structured metadata and dropped from indexed labels.

## Troubleshooting

### Monitoring

```bash
kubectl get pods -n monitoring                      # All monitoring pods
kubectl logs -n monitoring -l app.kubernetes.io/name=alloy   # Alloy metrics agent
helm get values alloy -n monitoring                 # Current Alloy config
kubectl get servicemonitors -A                      # All ServiceMonitors
kubectl get podmonitors -A                          # All PodMonitors
```

### Logging

```bash
kubectl get pods -n logging                         # All logging pods
kubectl get ds -n logging                           # DaemonSet status (should match node count)
kubectl logs -n logging -l app.kubernetes.io/name=alloy-logging --tail=20  # Recent Alloy logs
kubectl get configmap alloy-logging-config -n logging -o yaml  # Assembled pipeline config
kubectl get secret grafana-cloud-credentials -n logging  # Verify credentials exist
```

### Common issues

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| No metrics in Grafana Cloud | `grafana-cloud-credentials` secret missing in `monitoring` ns | Create the secret, redeploy |
| No logs in Grafana Cloud | `grafana-cloud-credentials` secret missing in `logging` ns | Create the secret, redeploy |
| Alloy logging pods not running | Secret missing — deploy exits cleanly | Create secret, run `just deploy-base <cluster>` |
| Missing logs from a namespace | Module `pipeline.alloy` has broken regex in `stage.match` | Check the assembled ConfigMap for syntax errors |
| Alloy OOMKilled | High-throughput nodes exceeding 1Gi limit | Increase `resources.limits.memory` in logging Helm values |
| RBAC errors in Alloy logs | ClusterRole collision between monitoring and logging Alloy | Verify `fullnameOverride: alloy-logging` in logging values |
| ServiceMonitor not discovered | CRD ordering — monitors applied before kube-prometheus-stack | Redeploy monitoring module (deploy.sh handles ordering) |
| Webhook job stuck Pending | Missing tolerations for `CriticalAddonsOnly` taint | Delete stuck job + Helm release, verify values, retry |
