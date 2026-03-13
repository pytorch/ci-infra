# modules/monitoring/ — Cluster Monitoring

Deploys kube-prometheus-stack (Prometheus, Grafana, AlertManager, node-exporter, kube-state-metrics) plus custom ServiceMonitors/PodMonitors for OSDC components and a DCGM exporter DaemonSet for GPU metrics.

## What's here

| Path | Purpose |
|------|---------|
| `deploy.sh` | Reads config, applies k8s resources, `helm upgrade --install` for kube-prometheus-stack, conditionally installs Alloy |
| `helm/values.yaml` | kube-prometheus-stack Helm values (node placement, auto-discovery, storage class) |
| `helm/alloy-values.yaml` | Grafana Alloy Helm values (controller type, Alloy config for ServiceMonitor/PodMonitor discovery + remote_write) |
| `kubernetes/namespace.yaml` | Monitoring namespace |
| `kubernetes/servicemonitors/` | ServiceMonitors for node-compactor, git-cache-central, Harbor exporter, ARC controller, Karpenter |
| `kubernetes/podmonitors/` | PodMonitors for git-cache DaemonSet, ARC listeners |
| `kubernetes/dcgm-exporter/` | DCGM exporter DaemonSet + headless Service + ServiceMonitor for GPU metrics |

## Configuration (clusters.yaml)

```yaml
defaults:
  monitoring:
    namespace: monitoring          # Kubernetes namespace
    retention_days: 15             # Prometheus data retention
    storage_size: 50Gi             # PVC size per Prometheus replica
    grafana_enabled: true          # Deploy Grafana
    alertmanager_enabled: true     # Deploy AlertManager
```

## Grafana Cloud push (optional)

If a `grafana-cloud-credentials` secret exists in the monitoring namespace, deploy.sh automatically installs **Grafana Alloy** to push metrics to Grafana Cloud. Alloy independently discovers ServiceMonitor/PodMonitor CRDs, scrapes the same targets as Prometheus, and pushes via `prometheus.remote_write`. The `grafana_cloud_url` in clusters.yaml provides the Mimir endpoint.

To enable: create the secret, set `grafana_cloud_url` in clusters.yaml, redeploy.
To disable: delete the secret and `helm uninstall alloy -n monitoring`.

## Dependencies

- Base must be deployed (Harbor running for image pulls)
- No terraform — monitoring is pure k8s/helm
- ServiceMonitors/PodMonitors target workloads from base (node-compactor, git-cache) and modules (ARC, Karpenter, Harbor)

## Key details

- Prometheus runs as an HA pair (2 replicas) on base infrastructure nodes
- node-exporter tolerates ALL taints to run on every node
- All other components run on base infrastructure nodes (tolerate `CriticalAddonsOnly`)
- Auto-discovers all ServiceMonitors/PodMonitors across all namespaces (`serviceMonitorSelectorNilUsesHelmValues: false`)
- DCGM exporter runs only on GPU nodes (nodeAffinity on `nvidia.com/gpu.present`)
