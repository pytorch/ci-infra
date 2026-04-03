# modules/monitoring/

## Key rules

- **All scrape intervals must be 60s** (Grafana recommendation — halves DPM billing). Only exception: arc-listeners at 3m. Do not add monitors below 60s without justification.
- **KSM metricRelabelings use RE2** — no `(?!...)` lookahead support. Use explicit `keep` allowlists instead.
- **Alloy is the only metrics pipeline** — no local Prometheus. PrometheusRule CRDs are synced to Grafana Cloud via `mimir.rules.kubernetes`.

## Metrics filtering layers

1. **`--metric-allowlist`** (KSM server-side) — controls which resource groups KSM generates
2. **`metricRelabelings`** (ServiceMonitor/PodMonitor) — `keep` whitelists per source
3. **Alloy `cost_control`** (before remote_write) — safety net for anything not filtered at source

## Bad node detection

`kube_pod_info` provides pod-to-node mapping. Join with error metrics to find bad nodes:

```promql
count(
  kube_pod_container_status_last_terminated_exitcode{container_exit_code!="0"}
  * on(namespace, pod) group_left(node) kube_pod_info
) by (node)
```
