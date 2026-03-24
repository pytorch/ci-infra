# modules/monitoring/ — Cluster Monitoring

Deploys monitoring infrastructure: kube-prometheus-stack for CRDs and exporters (node-exporter, kube-state-metrics), custom ServiceMonitors/PodMonitors for OSDC components, a DCGM exporter DaemonSet for GPU metrics, and Grafana Alloy to push metrics to Grafana Cloud.

Prometheus, Grafana, and AlertManager are **not installed locally** — all metrics go to Grafana Cloud via Alloy.

## What's here

| Path | Purpose |
|------|---------|
| `deploy.sh` | Installs kube-prometheus-stack (CRDs + exporters), applies monitors, conditionally installs Alloy |
| `helm/values.yaml` | kube-prometheus-stack values — Prometheus/Grafana/AlertManager disabled, exporters + operator enabled |
| `helm/alloy-values.yaml` | Grafana Alloy values — ServiceMonitor/PodMonitor discovery + remote_write to Grafana Cloud |
| `kubernetes/namespace.yaml` | Monitoring namespace |
| `kubernetes/monitors/` | CRD-dependent resources applied by deploy.sh after Helm install |
| `kubernetes/monitors/servicemonitors/` | ServiceMonitors for API server, node-compactor, git-cache-central, Harbor, ARC controller, Karpenter |
| `kubernetes/monitors/podmonitors/` | PodMonitors for CoreDNS, git-cache DaemonSet, ARC listeners |
| `kubernetes/dcgm-exporter/` | DCGM exporter DaemonSet + custom metrics ConfigMap (ServiceMonitor is in monitors/) |
| `kubernetes/alerts/` | PrometheusRule CRDs — ARC, infrastructure, and GPU alerts (synced to Grafana Cloud by Alloy's `mimir.rules.kubernetes`) |

## Configuration (clusters.yaml)

```yaml
defaults:
  monitoring:
    namespace: monitoring          # Kubernetes namespace
    grafana_cloud_url: "https://prometheus-prod-36-prod-us-west-0.grafana.net/api/prom/push"
```

## Metrics pipeline

**Grafana Alloy** is the primary (and only) metrics pipeline. It discovers ServiceMonitor/PodMonitor CRDs, scrapes targets, applies cost-control relabeling, and pushes to Grafana Cloud via `prometheus.remote_write`.

### Metrics cost control

A `prometheus.relabel "cost_control"` stage sits between discovery and remote_write. It drops high-cardinality, low-value metrics before they leave the cluster:

- **cadvisor**: network tcp/udp usage, tasks state, cpu load average, memory failures, blkio device usage, last_seen, start_time, spec_*
- **KSM**: *_created, *_metadata_resource_version, secret/configmap/endpoint/lease metrics
- **ARC histograms**: job execution/startup duration buckets (prevents unbounded series growth)

### DCGM custom metrics

The DCGM exporter uses a curated ~26 metric subset (ConfigMap mounted as `/etc/dcgm-exporter/custom-metrics.csv`) instead of the full ~200 default metrics. High-cardinality labels (UUID, modelName, DCGM_FI_DRIVER_VERSION, pci_bus_id) are dropped via `metricRelabelings` on the ServiceMonitor.

### Alerting via mimir.rules.kubernetes

PrometheusRule CRDs in `kubernetes/alerts/` are **not evaluated locally** (no Prometheus instance). Instead, Alloy's `mimir.rules.kubernetes` component syncs them to Grafana Cloud Mimir, which evaluates the rules and routes alerts through Grafana Cloud Alerting.

Alloy is installed when a `grafana-cloud-credentials` secret exists in the monitoring namespace.

To enable: create the secret and redeploy.
To disable: delete the secret and `helm uninstall alloy -n monitoring`.

## What kube-prometheus-stack provides

The chart is used only as a CRD + exporter bundle:
- **CRDs**: `monitoring.coreos.com` (ServiceMonitor, PodMonitor, PrometheusRule, etc.)
- **Prometheus Operator**: Manages CRD lifecycle
- **node-exporter**: DaemonSet on every node (system metrics)
- **kube-state-metrics**: Kubernetes object metrics

Prometheus, Grafana, and AlertManager are all `enabled: false`.

## Deploy ordering

1. Justfile applies `kubernetes/kustomization.yaml` (namespace + DCGM DaemonSet)
2. `deploy.sh` installs kube-prometheus-stack (CRDs + exporters)
3. `deploy.sh` applies `kubernetes/monitors/` (ServiceMonitors + PodMonitors — requires CRDs from step 2)
4. `deploy.sh` conditionally installs Alloy (if `grafana-cloud-credentials` secret exists)

## Log parsing pipeline

This module can contribute log parsing rules for the centralized logging system (`base/logging/`). Place a `logging/pipeline.alloy` file containing `stage.match` blocks in this module directory. The logging `assemble_config.py` script discovers it at deploy time and inserts the blocks into the base Alloy config. See `base/logging/CLAUDE.md` and `docs/observability.md` for details.

## Credential setup

The Grafana Cloud URLs come from `clusters.yaml` (`monitoring.grafana_cloud_url` for write, `monitoring.grafana_cloud_read_url` for read), not from secrets. Secrets only contain authentication credentials.

**Write credentials** (used by Alloy to push metrics):

```bash
kubectl create namespace monitoring
kubectl create secret generic grafana-cloud-credentials \
  -n monitoring \
  --from-literal=username='<GRAFANA_CLOUD_METRICS_USER_ID>' \
  --from-literal=password='<API_KEY_WITH_METRICS_WRITE_SCOPE>' \
  --from-literal=loki-username='<GRAFANA_CLOUD_LOKI_USER_ID>' \
  --from-literal=loki-api-key-write='<API_KEY_WITH_LOGS_WRITE_SCOPE>' \
  --from-literal=loki-api-key-read='<API_KEY_WITH_LOGS_READ_SCOPE>'
```

**Read credentials** (used by smoke tests to verify metrics arrive in Mimir):

```bash
kubectl create secret generic grafana-cloud-read-credentials \
  -n monitoring \
  --from-literal=username='<GRAFANA_CLOUD_METRICS_USER_ID>' \
  --from-literal=password='<API_KEY_WITH_METRICS_READ_SCOPE>'
```

## Dependencies

- Base must be deployed (Harbor running for image pulls)
- No terraform — monitoring is pure k8s/helm
- ServiceMonitors/PodMonitors target workloads from base (node-compactor, git-cache) and modules (ARC, Karpenter, Harbor)

## Key details

- node-exporter tolerates ALL taints to run on every node
- All other components run on base infrastructure nodes (tolerate `CriticalAddonsOnly`)
- DCGM exporter runs only on GPU nodes (nodeAffinity on `nvidia.com/gpu.present`)
- Alloy runs as 2 replicas with clustering for dedup
