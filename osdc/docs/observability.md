# Observability: Monitoring + Logging

OSDC has two observability pipelines, both pushing telemetry to Grafana Cloud. They use **three Grafana Alloy installations** with distinct modes, namespaces, and RBAC to avoid collisions.

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
                        ┌───────┴──────┐ ┌──────┴────────┐ ┌──────────────┐
                        │ Alloy        │ │ Alloy         │ │ Alloy        │
                        │ (Deployment) │ │ (DaemonSet)   │ │ (Deployment) │
                        │ 2 replicas   │ │ 1 per node    │ │ 1 replica    │
                        │ ns:monitoring│ │ ns:logging    │ │ ns:logging   │
                        │ clustered    │ │ independent   │ │ independent  │
                        └───────┬──────┘ └──────┬────────┘ └──────┬───────┘
                                │               │                 │
                   ServiceMonitor/   loki.source.file +   loki.source.
                   PodMonitor CRDs   loki.source.journal  kubernetes_events
                        │               │                 │
                  ┌─────┴─────────┐  ┌──┴──────────────┐  K8s Events API
                  │ kube-prom-stack│  │ /var/log/pods/  │
                  │ exporters:    │  │ /var/log/journal │
                  │ - node-export │  │ (every node)    │
                  │ - kube-state  │  └─────────────────┘
                  │ - kubelet/    │
                  │   cAdvisor    │
                  │ - operator    │
                  └───────────────┘
```

## The Three Alloy Installations

| Aspect | Monitoring Alloy | Logging Alloy | Events Alloy |
|--------|-----------------|---------------|--------------|
| **Location** | `modules/monitoring/` (opt-in module) | `modules/logging/` (opt-in module) | `modules/logging/` (opt-in module) |
| **Controller type** | Deployment (2 replicas) | DaemonSet (1 per node) | Deployment (1 replica) |
| **Namespace** | `monitoring` | `logging` | `logging` |
| **Helm release** | `alloy` | `alloy-logging` | `alloy-events` |
| **fullnameOverride** | (default) | `alloy-logging` | `alloy-events` |
| **Clustering** | Enabled (HA target dedup) | Disabled (each pod handles its node) | Disabled |
| **Config source** | Helm-generated (alloy-values.yaml) | Assembled ConfigMap (`assemble_config.py`) | Inline in Helm values |
| **Data destination** | Grafana Cloud Mimir (metrics) | Grafana Cloud Loki (logs) | Grafana Cloud Loki (logs) |
| **Secret name** | `grafana-cloud-credentials` in `monitoring` | `grafana-cloud-credentials` in `logging` | `grafana-cloud-credentials` in `logging` |
| **Secret keys** | `username`, `password` (URL from `clusters.yaml`) | `loki-username`, `loki-api-key-write` (Alloy uses write only; `loki-api-key-read` is required by `docs/loki_query.md` tooling, not Alloy) | Same as Logging Alloy |

### Why three separate installations?

1. **Different controller types** — metrics scraping works with a clustered Deployment (Alloy's built-in clustering distributes scrape targets). Log collection requires a DaemonSet (each node's logs are local files). Event collection needs a single-replica Deployment (to avoid duplicate events).
2. **RBAC isolation** — each Alloy needs ClusterRole/ClusterRoleBinding for Kubernetes API access. Without `fullnameOverride`, Helm creates identically-named RBAC resources that collide.
3. **Independent lifecycle** — metrics and logs can be enabled/disabled separately. Both logging and monitoring are modules (opt-in).
4. **Config complexity** — monitoring Alloy config is driven by CRD discovery (ServiceMonitor/PodMonitor). Logging Alloy config is assembled from base + per-module pipeline files. Events Alloy uses inline config for `loki.source.kubernetes_events`. Mixing these in one config would be fragile.

## Monitoring Pipeline (Metrics)

### What kube-prometheus-stack provides

The chart (v82.10.3) is used **only as a CRD + exporter bundle**:

- **CRDs**: `monitoring.coreos.com` — ServiceMonitor, PodMonitor, PrometheusRule, etc.
- **Prometheus Operator**: Manages CRD lifecycle
- **node-exporter**: DaemonSet on every node (tolerates ALL taints), 60s scrape interval
- **kube-state-metrics**: Kubernetes object state metrics (runs on base nodes)

**Prometheus, Grafana, and AlertManager are all `enabled: false`.** No in-cluster metric storage or dashboards. All metrics go to Grafana Cloud.

### What's scraped

| Type | Name | Target Namespace | What it monitors |
|------|------|-----------------|-----------------|
| ServiceMonitor | apiserver | default | K8s API server request counters (`apiserver_request_total`, `apiserver_request_terminations_total`) — latency, inflight, and etcd metrics are filtered out |
| ServiceMonitor | arc-controller | arc-systems | ARC controller metrics |
| ServiceMonitor | harbor | harbor-system | Harbor exporter metrics |
| ServiceMonitor | karpenter | karpenter | Karpenter controller metrics |
| ServiceMonitor | node-compactor | kube-system | Node compactor metrics |
| ServiceMonitor | dcgm-exporter | monitoring | NVIDIA GPU metrics (DCGM) |
| ServiceMonitor | buildkit | buildkit | BuildKit daemon metrics |
| ServiceMonitor | buildkit-haproxy | buildkit | BuildKit HAProxy LB metrics |
| ServiceMonitor | pushgateway | monitoring | Prometheus Pushgateway (push-based metrics from short-lived jobs) |
| ServiceMonitor | pypi-cache | pypi-cache | pypi-cache nginx metrics (`nginx_up`, requests, active connections) |
| PodMonitor | coredns | kube-system | CoreDNS request rate, latency, cache hit/miss, errors |
| PodMonitor | nodelocaldns | kube-system | NodeLocal DNSCache — two endpoints: `:9253` (CoreDNS plugin metrics, `coredns_*`) and `:9353` (binary-emitted `coredns_nodecache_*` setup/error counters) |
| PodMonitor | arc-listeners | arc-systems | ARC listener pods metrics |

Plus kube-prometheus-stack built-in targets:
- **node-exporter** — DaemonSet on ALL nodes (tolerates all taints), 60s interval; heavily filtered (see "Layer 2" below — only `node_memory_MemAvailable_bytes` and `node_memory_MemTotal_bytes` are kept)
- **kube-state-metrics** — Deployment on base-infrastructure nodes

The built-in kubelet ServiceMonitor is currently disabled — see [Disabled scrapers (IPv6 migration)](#disabled-scrapers-ipv6-migration).

### Disabled scrapers (IPv6 migration)

The kubelet ServiceMonitor (`/metrics` + cAdvisor) is **disabled** in `modules/monitoring/helm/values.yaml` (`kubelet.enabled: false`).

**Why**: Prometheus Operator manages the kubelet Endpoints object directly (unlike a normal ServiceMonitor that reads the Service's auto-built EndpointSlice), and its address picker (`pkg/kubelet/controller.go`) returns the first `NodeInternalIP` from `node.status.addresses`. On EKS dual-stack nodes the IPv4 InternalIP is listed first, so the Endpoints contain IPv4 addresses exclusively. Alloy runs in IPv6-only pods and cannot reach those endpoints — every scrape times out and the series silently disappear from Mimir.

**What this drops** (until re-enabled):
- `container_memory_working_set_bytes` / `container_memory_rss` (cAdvisor)
- `kubelet_running_pods` / `kubelet_running_containers`

These provide the per-pod memory series consumed by the `cost_control` Alloy rule's control-plane scoping.

**TODO (re-enable)**: Ship a custom IPv6-aware kubelet ServiceMonitor under `modules/monitoring/kubernetes/monitors/servicemonitors/`. The clean path is `kubernetes_sd_configs role: node` plus a relabel on `__meta_kubernetes_node_address_InternalIP` to select the IPv6 entry, then flip `kubelet.enabled: true` (and uncomment the filter block) in `helm/values.yaml`.

### Cost-control filtering (three layers)

Filtering happens at three layers, each closer to the source than the previous:

1. **`--metric-allowlist` (KSM server-side)** — controls which resource groups KSM generates at all.
2. **ServiceMonitor / PodMonitor `metricRelabelings`** — `keep` whitelists per source. This is where most per-target filtering happens.
3. **Alloy `prometheus.relabel "cost_control"`** — final safety net before `remote_write`, catches anything not filtered at source.

#### Layer 1: KSM metric allowlist

Only these resource types are emitted by kube-state-metrics:

```
kube_(daemonset|deployment|pod|namespace|node|statefulset|persistentvolume|
      horizontalpodautoscaler|job)_.+
```

(No `replicaset` group — ReplicaSet metrics are intentionally excluded.)

A second `keep` rule on the KSM ServiceMonitor narrows further (e.g. `kube_daemonset_status_(desired_number_scheduled|number_ready|...)`, only specific deployment fields, only error/restart pod metrics) and several `drop` rules strip routine reasons (`Completed`, `Shutdown`, `NodeAffinity`) and successful exit codes.

#### Layer 2: ServiceMonitor / PodMonitor `metricRelabelings`

Per-source `keep` whitelists or targeted `drop` rules — most filtering lives here. Examples (not exhaustive):

| Source | Filter |
|--------|--------|
| `apiserver` ServiceMonitor | `keep`: `apiserver_request_total\|apiserver_request_terminations_total` only — request rate and termination counts only; latency, inflight, and etcd metrics are dropped |
| `coredns` PodMonitor | `drop`: `go_.*`, `process_.*`, `coredns_dns_request_duration_seconds_bucket\|coredns_forward_request_duration_seconds_bucket` |
| `pypi-cache` ServiceMonitor | `keep`: `nginx_up\|nginx_http_requests_total\|nginx_connections_active` only |
| node-exporter (built-in) | `keep`: `node_memory_MemAvailable_bytes\|node_memory_MemTotal_bytes\|node_nf_conntrack_entries\|node_nf_conntrack_entries_limit` only — drops everything else including all CPU, disk I/O, filesystem, network, and load metrics. Conntrack metrics are kept because they power the `ipv6-network-pressure` alerts (pod-to-IPv4 egress via VPC CNI SNAT is conntrack-heavy under IPv6-only EKS). |
| `buildkit` ServiceMonitor | `drop`: `go_.*`, `process_.*`, `promhttp_.*\|target_info`, `.*_bucket` — drop-list pattern (not keep-list); buckets are dropped wholesale, only `_sum`/`_count` survive for latency tracking |
| DCGM ServiceMonitor | label drops: `UUID`, `modelName`, `DCGM_FI_DRIVER_VERSION`, `pci_bus_id` |

#### Layer 3: Alloy `cost_control` (safety net)

Final drops applied to anything that escapes layers 1–2:

| Rule | What it does |
|------|----|
| KSM low-value drop | Drops `kube_.*_created`, `kube_.*_metadata_resource_version`, `kube_secret_.*`, `kube_configmap_.*`, `kube_endpoint_.*`, `kube_lease_.*` |
| Control-plane scoping (load-bearing) | Two-step replace+drop: `kube_pod_container_status_restarts_total`, `container_memory_working_set_bytes`, `container_memory_rss` are KEPT only for namespaces `arc-systems\|karpenter\|harbor-system\|monitoring\|logging\|buildkit` and DROPPED for all others. RE2 has no lookahead so the rule tags survivors with `__keep_cp__="true"` then drops anything matching but not tagged. The `arc-runners` exclusion is intentional cost control — alerting (e.g. `ControlPlaneCrashLoop`) only fires on these namespaces. |
| Misc high-cardinality drop | Drops `kubernetes_feature_enabled` |
| ARC histogram buckets | Drops `gha_job_(execution\|startup)_duration_seconds_bucket` (sum/count kept) |
| Runtime/operator internals | Drops `go_.*`, `process_.*`, `promhttp_.*`, `prometheus_operator_.*` from any source |

**Node-exporter disabled collectors** (Layer 1, via `--no-collector.*` extra args): bcache, bonding, infiniband, nfs, nfsd, fibrechannel, ipvs, rapl, schedstat, interrupts. Filesystem and netdev also have exclusion patterns for virtual filesystems and virtual network interfaces.

### Alerting

PrometheusRule CRDs are defined locally and synced to Grafana Cloud Mimir via Alloy's `mimir.rules.kubernetes` component for remote evaluation. Eight alert groups across eight PrometheusRule files in `kubernetes/alerts/`:

| Group | Alert | Condition |
|-------|-------|-----------|
| arc | `ARCListenerStall` | Assigned jobs stuck for 15m |
| gpu | `GPUDoublebitECCError` | Uncorrectable memory errors |
| gpu | `GPUXIDCriticalError` | XID 48/79/94/95 errors |
| gpu | `GPURowRemapFailure` | Row remapping failed |
| gpu | `GPUTemperatureCritical` | GPU temp >95C for 5m |
| infrastructure | `NodeNotReady` | Node not ready for 5m |
| infrastructure | `ControlPlaneCrashLoop` | >5 restarts/hr in control-plane namespaces |
| infrastructure | `HarborDown` | Harbor not responding for 5m |
| infrastructure | `KarpenterNodeClaimNotReady` | NodeClaim created but no node joins for 15m |
| node-compactor | `NodeCompactorReconcileErrors` | Continuous reconciliation errors for 15m (burst-absorption offline) |
| zombie-cleanup | `ZombieCleanupCapReached` | Per-round cap reached — zombie pods deferred to next run |
| harbor-cache-recovery | `HarborCacheRecoveryFailing` | Cache-recovery CronJob failing for 15m (≥3 consecutive runs at */5). Targets `kube_job_status_failed` — each scheduled run is a fresh Job object, no long-lived pod to watch. |
| harbor-cache-recovery | `HarborCacheRecoveryOOM` | Recovery pod OOMKilled (most common root cause — listing pods cluster-wide). Uses `kube_pod_container_status_terminated_reason` (no `_last_`) because recovery pods have `restartPolicy=Never`; KSM only populates `_last_terminated_reason` after a container restart. |
| harbor-cache-recovery | `HarborCacheRecoveryStale` | No successful recovery run in >30m (CronJob suspended/stuck). Targets `kube_job_status_completion_time` for the same Job-vs-Pod reason. |
| nodelocaldns | `NodeLocalDNSSetupErrors` | `increase(coredns_nodecache_setup_errors_total[5m]) > 0` — iptables NOTRACK rule install failed |
| nodelocaldns | `NodeLocalDNSPodRestarting` | NLD container restarting (per-node DNS interception briefly degrades to fallthrough) |
| nodelocaldns | `NodeLocalDNSDaemonSetDegraded` | DaemonSet has unavailable pods for >15m |
| ipv6-network-pressure | `NodeConntrackHighWarn` | `node_nf_conntrack_entries / node_nf_conntrack_entries_limit > 0.80` for 5m — pod-to-IPv4 egress on IPv6-only EKS goes via VPC CNI's IPv4 SNAT, which is conntrack-heavy |
| ipv6-network-pressure | `NodeConntrackHighCritical` | Same ratio > 0.95 for 2m — new connections will start failing imminently; raise `nf_conntrack_max` or drain the node |
| ipv6-network-pressure | `NodeConntrackMetricMissing` | `absent(node_nf_conntrack_entries)` for 10m — catches silent loss of node-exporter scraping (e.g. IPv6 endpoint regression); without it the conntrack alerts above would be inert |

### Adding a new ServiceMonitor/PodMonitor

1. Create the manifest in `modules/monitoring/kubernetes/monitors/servicemonitors/` or `podmonitors/`
2. Add it to `modules/monitoring/kubernetes/monitors/kustomization.yaml`
3. Redeploy: `just deploy-module <cluster> monitoring`

Monitors are applied by `deploy.sh` after kube-prometheus-stack Helm install (CRDs must exist first).

### Credential setup (monitoring)

The Grafana Cloud URL comes from `clusters.yaml` (`monitoring.grafana_cloud_url`), not from the secret. Two separate secrets are used in the `monitoring` namespace — one for Alloy's write path, one for the smoke tests' Mimir read queries.

**Write credentials** — required for Alloy. The `monitoring/deploy.sh` script gates Alloy install on this secret existing:

```bash
kubectl create namespace monitoring
kubectl create secret generic grafana-cloud-credentials \
  -n monitoring \
  --from-literal=username='<GRAFANA_CLOUD_METRICS_USER_ID>' \
  --from-literal=password='<API_KEY_WITH_METRICS_WRITE_SCOPE>'
```

**Read credentials** — required for `just smoke` to verify metrics actually arrived in Mimir. Without it, the remote-verification tests in `modules/monitoring/tests/smoke/test_monitoring.py` skip silently, leaving `remote_write` regressions undetected:

```bash
kubectl create secret generic grafana-cloud-read-credentials \
  -n monitoring \
  --from-literal=username='<GRAFANA_CLOUD_METRICS_USER_ID>' \
  --from-literal=password='<API_KEY_WITH_METRICS_READ_SCOPE>'
```

### Configuration (clusters.yaml)

```yaml
defaults:
  monitoring:
    namespace: monitoring
    grafana_cloud_url: "https://prometheus-prod-36-prod-us-west-0.grafana.net/api/prom/push"
    grafana_cloud_read_url: "https://prometheus-prod-36-prod-us-west-0.grafana.net/api/prom"
```

## Metrics Cardinality Reference (meta-prod-aws-ue2)

See [observability-estimates.md](observability-estimates.md#metrics-cardinality-reference) for detailed per-pod, per-node, and cluster-wide metrics cardinality estimates.

## Logging Pipeline (Logs)

### Log sources

1. **Pod logs** — `loki.source.file` reads CRI-format logs from `/var/log/pods/` on each node (Alloy DaemonSet)
2. **System journal** — `loki.source.journal` reads kubelet, containerd, kernel, nvidia-fabricmanager, and nvidia-persistenced logs from `/var/log/journal` (Alloy DaemonSet)
3. **Kubernetes events** — `loki.source.kubernetes_events` watches the K8s Events API and pushes events as JSON log lines (Alloy Events Deployment, single replica). The events Alloy is more than a passthrough: it does its own JSON extraction (`reason`, `type`, `kind`, `sourcecomponent`), drops events from the `logging` namespace (feedback-loop prevention), promotes `type` and `kind` to labels, and attaches `reason` and `sourcecomponent` as structured metadata.

### Base pipeline drops

The base pipeline applies hard drops that affect every log line — these run before module pipelines and the final rate-limit:

- **Completed pods** — drop pods in `Succeeded|Failed` phase (target relabel before `loki.source.file`).
- **`logging` namespace** — drop all pods in the `logging` namespace (feedback-loop prevention; Alloy's own log shipping must not be ingested).
- **DEBUG/TRACE lines** — `stage.drop` with case-insensitive level token match (`drop_counter_reason="low_log_level"`).
- **Oversized lines** — drop lines >16KB (`drop_counter_reason="oversized_line"`).
- **Health probes** — drop lines containing `kube-probe/`.
- **Journal debug** — drop journal entries with priority 7 (`drop_counter_reason="journal_debug"`).

### Level normalization

After module pipelines run (which may extract a `level` value), the base pipeline normalizes any extracted `level`:

1. Lowercases the value (e.g. `ERROR` → `error`, `WARN` → `warn`).
2. Maps short aliases to canonical names: `err|e` → `error`, `warning|w` → `warn`, `information|i` → `info`, `dbg|d` → `debug`, `f` → `fatal`.

Modules that extract `level` get this normalization for free.

### Rate limiting

After module pipelines and level normalization, a global per-pod rate-limit applies: `stage.limit { rate = 1000, burst = 5000, by_label_name = "pod" }`. This is a safety valve — well-behaved noisy sources should be sampled or rate-limited in their module pipeline so important logs are protected before this limiter fires.

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
2. For each module (except `logging` itself, which owns the base pipeline), checks `modules/<name>/logging/pipeline.alloy`
3. **Consumer/upstream overlay**: the assembler is invoked with both `--modules-dir` (consumer, `OSDC_ROOT/modules`) and `--upstream-modules-dir` (`OSDC_UPSTREAM/modules`). The consumer path is checked first; if absent, it falls back to upstream. An empty/whitespace-only consumer file is an explicit **opt-out** — the upstream pipeline is NOT used as a fallback in that case, allowing consumers to suppress a module's logging pipeline.
4. Inserts all discovered blocks at the `// MODULE_PIPELINES` marker in `base.alloy`
5. Outputs the assembled config as a ConfigMap YAML (`alloy-logging-config`)

**Example module pipeline** (`modules/karpenter/logging/pipeline.alloy`):

```alloy
// Karpenter (zap JSON logs)
stage.match {
    selector = "{namespace=\"karpenter\"}"
    stage.json {
        expressions = {
            level = "level",
        }
    }
    stage.labels {
        values = {
            level = "",
        }
    }
}
```

**Per-module sampling and rate limits** — module pipelines do more than parsing; they also enforce volume controls before the base pipeline's global rate-limit:

| Module | Behavior |
|--------|----------|
| `arc` (controller) | Logfmt parse, extract `level` |
| `arc` (runners) | Extract `level`, then **sample non-error logs at 10%**. `workflow_run_id` regex extraction runs only on the survivors and is promoted to structured metadata. Errors/warns/fatals/panics always kept. |
| `buildkit` | Logfmt parse, extract `level`, then **sample non-error logs at 50%**. |
| `monitoring` | Parses Prometheus operator JSON, kube-state-metrics klog, Harbor JSON, Harbor nginx access logs (drops `/health` probes, extracts status). **Rate-limits non-error `harbor-system` logs** (`stage.limit` rate=100, burst=500). |
| `karpenter` | Extract `level` from zap JSON (the example above). |

### Credential setup (logging)

```bash
kubectl create namespace logging
kubectl create secret generic grafana-cloud-credentials \
  -n logging \
  --from-literal=loki-username='<GRAFANA_CLOUD_LOKI_USER_ID>' \
  --from-literal=loki-api-key-write='<API_KEY_WITH_LOGS_WRITE_SCOPE>' \
  --from-literal=loki-api-key-read='<API_KEY_WITH_LOGS_READ_SCOPE>'
```

Alloy (DaemonSet + events Deployment) only consumes `loki-username` and `loki-api-key-write`. The `loki-api-key-read` key is consumed by the read tooling described in `docs/loki_query.md` — it must exist in the cluster but is not mounted into Alloy.

### Configuration (clusters.yaml)

```yaml
defaults:
  logging:
    namespace: logging
    grafana_cloud_loki_url: "https://logs-prod-021.grafana.net/loki/api/v1/push"
```

## Log Volume Estimation Reference (meta-prod-aws-ue2)

See [observability-estimates.md](observability-estimates.md#log-volume-estimation-reference) for per-unit log volume rates and scaling formulas. Runner pod logs are expected to be the dominant source despite 90% sampling of non-error lines.

## Deploy Order

### Monitoring (module — runs during `deploy-module`)

1. Justfile applies `kubernetes/kustomization.yaml` (namespace + DCGM DaemonSet)
2. `deploy.sh` installs kube-prometheus-stack (CRDs + exporters), chart `v82.10.3`
3. `deploy.sh` installs Prometheus Pushgateway (chart `v3.6.0`, runs on base-infrastructure, scraped by the `pushgateway` ServiceMonitor)
4. `deploy.sh` applies `kubernetes/monitors/` (ServiceMonitors, PodMonitors, and PrometheusRules — alerts are included via kustomization, requires CRDs from step 2)
5. `deploy.sh` conditionally installs Alloy (chart version from `clusters.yaml` `alloy_chart_version`, currently `1.6.2`) if `grafana-cloud-credentials` secret exists. After every successful `helm upgrade`, `deploy.sh` runs `kubectl rollout restart deployment/alloy` followed by `rollout status` — this picks up secret rotations and external config changes that Helm doesn't trigger naturally (the secret keys are referenced via `valueFrom.secretKeyRef`, so Kubernetes won't restart pods when the secret value changes).

### Logging (module — runs during `deploy-module`)

Deployed via `just deploy-module <cluster> logging` (or as part of `just deploy <cluster>`).

1. Creates `logging` namespace
2. Gates on `grafana-cloud-credentials` secret (exits cleanly if missing)
3. Runs `assemble_config.py` to build the ConfigMap from base + module pipelines
4. `kubectl apply` the ConfigMap
5. `helm upgrade --install alloy-logging` with runtime env vars (cluster name, Loki URL, credentials)
6. `helm upgrade --install alloy-events` for Kubernetes event collection

## Gotchas

### RBAC collision

All three Alloy installations create ClusterRole/ClusterRoleBinding resources. Without `fullnameOverride` (`alloy-logging`, `alloy-events`), Helm would create identically-named resources that collide with each other and with the monitoring Alloy.

### CRD ordering

ServiceMonitor/PodMonitor CRDs don't exist until kube-prometheus-stack installs. The monitoring module's `deploy.sh` applies monitors in a separate step after Helm install. Don't put ServiceMonitor/PodMonitor manifests in the main `kubernetes/kustomization.yaml` — they'll fail before CRDs exist.

### Admission webhook

kube-prometheus-stack's Prometheus Operator admission webhook pre-install job has no tolerations by default. On OSDC clusters where ALL base nodes are tainted with `CriticalAddonsOnly`, the job stays Pending forever. The Helm values include tolerations for `prometheusOperator.admissionWebhooks.patch`. If the job gets stuck, delete the failed Helm release and the stuck job before retrying.

### Alloy memory on high-throughput nodes

CI nodes can have 100+ concurrent runner pods each producing logs. The logging Alloy DaemonSet is configured with 1Gi request / 2Gi limit and GC tuning (`GOGC=200`, `GOMEMLIMIT=1800MiB`). Under sustained backpressure (Loki down, rate-limited), Alloy may still OOM on high-throughput nodes. Check `kubectl describe pod` for OOMKilled events. The `stage.limit` rate limiter (1000 lines/s, burst 5000, scoped `by_label_name = "pod"` so each source pod is rate-limited independently) helps bound memory usage.

### `NODE_NAME` env var (logging DaemonSet)

`deploy.sh` injects `NODE_NAME` into the logging Alloy pod from `spec.nodeName` via the downward API. The base pipeline depends on it for two things: the pod-discovery field selector (`field = "spec.nodeName=" + env("NODE_NAME")`) so each DaemonSet pod only discovers pods on its own node, and as the `node` static label on journal entries. If `NODE_NAME` is not set, pod discovery and journal labelling break.

### Module pipeline ordering

The `// MODULE_PIPELINES` marker in `base.alloy` is placed **before** `stage.structured_metadata` and `stage.label_drop`. This means module `stage.match` blocks can select on `{pod=~"..."}` and `{node=~"..."}` labels. After the module pipelines run, pod and node are moved to structured metadata and dropped from indexed labels.

### IPv6 binding overrides (IPv6-only EKS)

Under IPv6-only EKS the kubelet readiness probe targets the pod's IPv6 address. Several upstream Helm chart defaults bind to `0.0.0.0` (IPv4-only) and would leave pods stuck `NotReady` and metrics silently missing:

- **`alloy-logging` and `alloy-events`** — both set `alloy.listenAddr: "[::]"` (in `modules/logging/helm/alloy-logging-values.yaml` and `alloy-events-values.yaml`). Without this the DaemonSet/Deployment never passes readiness.
- **`node-exporter`** — `prometheus-node-exporter.listenOnAllInterfaces: false` (in `modules/monitoring/helm/values.yaml`), which makes node-exporter bind the node's primary IP from Downward API `status.hostIP` instead of `0.0.0.0:9100`. Without it Alloy (IPv6-only pod) cannot scrape node-exporter and `node_nf_conntrack_entries` silently disappears — which would in turn make the `ipv6-network-pressure` alerts inert (the `NodeConntrackMetricMissing` alert exists precisely to catch this regression).

These overrides are easy to miss when adding a new Alloy chart instance or another `:0.0.0.0`-binding exporter — anything that needs to be scraped or probed from the IPv6 pod network must bind IPv6.

## Troubleshooting

### Monitoring

```bash
kubectl get pods -n monitoring                      # All monitoring pods
kubectl logs -n monitoring -l app.kubernetes.io/name=alloy   # Alloy metrics agent
helm get values alloy -n monitoring                 # Current Alloy config
kubectl get servicemonitors -A                      # All ServiceMonitors
kubectl get podmonitors -A                          # All PodMonitors
kubectl get prometheusrules -A                      # All PrometheusRules
```

### Logging

```bash
kubectl get pods -n logging                         # All logging pods (DaemonSet + Events Deployment)
kubectl get ds -n logging                           # DaemonSet status (should match node count)
kubectl logs -n logging -l app.kubernetes.io/name=alloy-logging --tail=20  # Log collector
kubectl logs -n logging -l app.kubernetes.io/name=alloy-events --tail=20  # Event collector
kubectl get configmap alloy-logging-config -n logging -o yaml  # Assembled pipeline config
kubectl get secret grafana-cloud-credentials -n logging  # Verify credentials exist
```

### Common issues

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| No metrics in Grafana Cloud | `grafana-cloud-credentials` secret missing in `monitoring` ns | Create the secret, redeploy |
| No logs in Grafana Cloud | `grafana-cloud-credentials` secret missing in `logging` ns | Create the secret, redeploy |
| No K8s events in Loki | `alloy-events` pod not running or secret missing | Check `kubectl get pods -n logging`, verify secret |
| Alloy logging pods not running | Secret missing — deploy exits cleanly | Create secret, run `just deploy-module <cluster> logging` |
| Missing logs from a namespace | Module `pipeline.alloy` has broken regex in `stage.match` | Check the assembled ConfigMap for syntax errors |
| Alloy OOMKilled | High-throughput nodes exceeding 2Gi limit | Increase `resources.limits.memory` in logging Helm values |
| RBAC errors in Alloy logs | ClusterRole collision between monitoring and logging Alloy | Verify `fullnameOverride` on all three Alloy releases |
| ServiceMonitor not discovered | CRD ordering — monitors applied before kube-prometheus-stack | Redeploy monitoring module (deploy.sh handles ordering) |
| Webhook job stuck Pending | Missing tolerations for `CriticalAddonsOnly` taint | Delete stuck job + Helm release, verify values, retry |
