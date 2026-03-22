# base/logging/ — Centralized Log Collection

Deploys Grafana Alloy in two modes: a DaemonSet for pod logs and system journal collection from every node, and a single-replica Deployment for Kubernetes Event collection. Both push to Grafana Cloud Loki. Only installed when a `grafana-cloud-credentials` secret exists in the logging namespace.

## What's here

| Path | Purpose |
|------|---------|
| `deploy.sh` | Secret-gated Alloy install (DaemonSet for logs + Deployment for events) |
| `pipelines/base.alloy` | Base Alloy River config — pod log collection, journal collection, loki.write output |
| `helm/alloy-logging-values.yaml` | Helm values for DaemonSet mode Alloy (tolerates all taints, journal mount, positions hostPath) |
| `helm/alloy-events-values.yaml` | Helm values for single-replica Deployment Alloy (K8s event collection) |
| `scripts/python/assemble_config.py` | Assembles base pipeline + per-module `stage.match` blocks into a ConfigMap |
| `scripts/python/test_assemble_config.py` | Unit tests for assembly logic |
| `tests/smoke/test_logging.py` | Post-deploy smoke tests (namespace, DaemonSet health, ConfigMap) |

## Configuration (clusters.yaml)

```yaml
defaults:
  logging:
    namespace: logging
    grafana_cloud_loki_url: "https://logs-prod-021.grafana.net/loki/api/v1/push"
```

## Log sources

1. **Pod logs** — `loki.source.file` reads CRI-format logs from `/var/log/pods/` on each node (DaemonSet)
2. **System journal** — `loki.source.journal` reads kubelet, containerd, kernel, and NVIDIA GPU service logs from `/var/log/journal` (DaemonSet)
3. **Kubernetes events** — `loki.source.kubernetes_events` watches the K8s Events API and pushes events as JSON log lines (single-replica Deployment, `alloy-events`)

## Label strategy

| Label | Type | Purpose |
|-------|------|---------|
| `cluster` | external_label | Cluster-scoped queries |
| `namespace` | indexed | Service scoping |
| `container` | indexed | Container identification |
| `app` | indexed | From `app.kubernetes.io/name` pod label |
| `pod` | structured metadata | Queryable but not indexed (Loki 3.x) |
| `node` | structured metadata | Queryable but not indexed (Loki 3.x) |

## Module pipeline discovery

Each module can contribute log parsing rules by placing a `logging/pipeline.alloy` file in its module directory. The file must contain only `stage.match` blocks — syntactically valid inside a `loki.process` block.

During deploy, `assemble_config.py`:
1. Reads `clusters.yaml` to get the cluster's enabled modules
2. For each module, checks `$OSDC_ROOT/modules/<name>/logging/pipeline.alloy` (consumer override first)
3. Falls back to `$OSDC_UPSTREAM/modules/<name>/logging/pipeline.alloy`
4. Inserts all discovered blocks at the `// MODULE_PIPELINES` marker in `base.alloy`
5. Outputs the assembled config as a ConfigMap YAML

## Deploy order

Within `deploy-base`, logging runs after Harbor (so images pull through the cache) and before Node Compactor:

1. Terraform → 2. Mirror images → 3. Base k8s → 4. Harbor → **5. Logging** → 6. Node Compactor

## Credential setup (one-time per cluster)

```bash
kubectl create namespace logging
kubectl create secret generic grafana-cloud-credentials \
  -n logging \
  --from-literal=loki-username='<GRAFANA_CLOUD_LOKI_USER_ID>' \
  --from-literal=loki-api-key-write='<API_KEY_WITH_LOGS_WRITE_SCOPE>' \
  --from-literal=loki-api-key-read='<API_KEY_WITH_LOGS_READ_SCOPE>'
```

## Multi-Alloy architecture

The logging component deploys **two** Alloy Helm releases in the `logging` namespace:
- **`alloy-logging`** — DaemonSet for pod logs + journal (one pod per node)
- **`alloy-events`** — Deployment for K8s event collection (single replica on base node)

Both are **separate from** the monitoring module's Alloy Deployment (`modules/monitoring/`, namespace `monitoring`). All three have distinct Helm releases, RBAC, and config. See `docs/observability.md` for the full architecture explanation.

## RBAC isolation

Uses `fullnameOverride` on each Helm release (`alloy-logging`, `alloy-events`) to prevent ClusterRole/ClusterRoleBinding collision with each other and with the monitoring module's Alloy deployment.

## Adding a module pipeline

To add log parsing for a module:

1. Create `modules/<name>/logging/pipeline.alloy` containing `stage.match` blocks
2. The `selector` must use labels available at the `// MODULE_PIPELINES` marker point (namespace, container, app, pod, node)
3. Redeploy: `just deploy-base <cluster>` (the assembly script discovers the file automatically)

Consumer repos can override or suppress upstream pipelines by placing a file at the same path in their `modules/` directory. An empty file = opt-out (upstream pipeline is NOT used as fallback).

## Troubleshooting

- **Alloy not installed**: Check `kubectl get secret grafana-cloud-credentials -n logging` — deploy is skipped without it
- **Alloy OOM**: Default limit is 1Gi. High-throughput CI nodes may need more — check `kubectl describe pod` for OOMKilled
- **Missing logs from a namespace**: Check if the module has a `logging/pipeline.alloy` with a broken regex — bad `stage.match` blocks can silently drop logs
- **Rate limiting**: `stage.limit` caps at 10MB/s per Alloy pod. Check Alloy metrics for `loki_process_dropped_lines_total`
- **Journal path empty**: EKS AL2023 uses `/var/log/journal` — the hostPath uses `DirectoryOrCreate` so it won't fail, but no journal logs will appear if the path is wrong
