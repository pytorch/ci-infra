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

### What we collect

Most filtering is done at the ServiceMonitor level via `keep` whitelists. Alloy `cost_control` acts as a safety net for anything not filtered at the source.

#### Node-level (node-exporter)
- `node_cpu_seconds_total` (idle, iowait modes only) — node CPU utilization and IO wait
- `node_memory_MemAvailable_bytes` — available memory on node
- `node_memory_MemTotal_bytes` — total memory on node

#### Pod-level (cadvisor)
- `container_memory_working_set_bytes` — active memory per pod; K8s uses this for OOM kill decisions
- `container_memory_rss` — physical memory per pod (no cache); useful for spotting memory leaks
- **NOTE:** per-container series — high pod churn (e.g. runners) increases cardinality

#### Kubernetes state (kube-state-metrics)
- `kube_daemonset_*`, `kube_deployment_*`, `kube_namespace_*`, `kube_node_*`, `kube_statefulset_*`, `kube_persistentvolume_*`, `kube_horizontalpodautoscaler_*`, `kube_job_*` — full metrics
- `kube_pod_*` — **error-only:**
  - `container_status_last_terminated_reason` (non-Completed)
  - `container_status_last_terminated_exitcode` (non-zero)
  - `status_reason`
- **Dropped:** replicaset, `kube_node_status_addresses`, `*_created`, `*_metadata_resource_version`, secrets, configmaps, endpoints, leases

#### Kubelet
- `kubelet_running_pods` — number of running pods per node
- `kubelet_running_containers` — number of running containers per node
- `kubelet_node_name` — node name mapping

#### API server
- `apiserver_request_total` — request count by verb/resource/code (error rate)
- `apiserver_request_terminations_total` — terminated requests

#### ARC (Actions Runner Controller)
- `gha_controller_*` — runner scheduling, pending count, scale set status
- `controller_runtime_reconcile_errors_total` — controller errors
- `gha_job_*_sum/_count` — job duration averages (buckets dropped)

#### Karpenter
- `karpenter_nodes_*` — node count, status, allocatable resources
- `karpenter_nodepools_*` — nodepool limits and usage
- `karpenter_nodeclaims_*` — nodeclaim lifecycle
- `karpenter_interruption_*` — spot interruption events

#### Other services
- **BuildKit** — all metrics except go/process/promhttp internals and histogram buckets
- **BuildKit HAProxy** — all metrics except resolver, process internals, aggregate check status
- **Harbor** — all metrics except go/process/promhttp internals
- **CoreDNS** — all metrics except go/process internals and two histogram buckets (dns_request, forward_request); other histogram buckets (health, kubernetes, proxy) are not yet dropped (~170 series, low priority)
- **DCGM (GPU)** — curated ~26 GPU metrics, high-cardinality labels dropped
- **git-cache / node-compactor / ARC listeners** — all metrics (custom exporters, low volume)

### TODO
- **arc-listeners PodMonitor** returns no data — listener pods may not expose a metrics port or label selector may not match. Investigate and fix or remove.
- **CoreDNS** still has unfiltered histogram buckets (health, kubernetes, proxy) — ~170 series, low priority.
- **git-cache / node-compactor / ARC listeners** have no metricRelabelings — volume is low (~20-30 series each), not worth filtering yet.

### Metrics cost control

Filtering is applied in two layers:

1. **ServiceMonitor `metricRelabelings`** (at scrape time) — `keep` whitelists on node-exporter, cadvisor, kubelet, apiserver, karpenter; `drop` rules on KSM, buildkit, harbor, coredns
2. **Alloy `cost_control`** (before remote_write) — safety net for anything not filtered at source:
   - `kube_pod_*`, `kube_*_created`, `kube_*_metadata_resource_version`, secrets, configmaps, endpoints, leases
   - `kubernetes_feature_enabled`
   - `gha_job_*_bucket`
   - `go_*`, `process_*`, `promhttp_*`

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

This module can contribute log parsing rules for the centralized logging system (`modules/logging/`). Place a `logging/pipeline.alloy` file containing `stage.match` blocks in this module directory. The logging `assemble_config.py` script discovers it at deploy time and inserts the blocks into the Alloy config. See `modules/logging/CLAUDE.md` and `docs/observability.md` for details.

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
