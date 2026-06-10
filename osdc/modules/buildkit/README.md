# BuildKit module

Remote BuildKit build service: per-arch `buildkitd` Deployments behind an HAProxy
LB, on dedicated Karpenter NodePools. Clients build with
`buildctl --addr tcp://buildkitd-<arch>.buildkit:1234`.

Sizing is per-arch in `clusters.yaml` (`buildkit.{amd64,arm64}_{replicas,pods_per_node}`,
`*_instance_type`); pod CPU/mem is computed by `scripts/python/generate_buildkit.py`.

## Autoscaling (optional, `buildkit.autoscaling.enabled`)

Absorb bursts of concurrent builds without overloading existing pods, and scale
back to a small warm baseline when idle.

- **One build per pod** — HAProxy `server maxconn 1` (matches buildkitd
  `max-parallelism = 1`). Excess builds **queue** in HAProxy instead of stacking
  on a busy pod; as new pods register (DNS), queued builds flow onto them, so
  scaled-up pods never sit idle. `timeout queue` must stay set and large enough
  to outlast a node-provision cycle — if omitted it falls back to `timeout
  connect` (5s), which would abort queued builds before pods scale up.
- **In-cluster scale signal** — KEDA `ScaledObject` per arch, `metrics-api`
  scraping the LB's own metrics (`haproxy_backend_current_sessions`) — no external
  metrics backend.
- **Warm baseline** — `amd64_min` / `arm64_min` keep ≥1 node per arch up so the
  common case gets a free warm pod immediately. `*_max` caps the burst; NodePool
  limits are sized to `*_max`.
- **Safe scale-down** — `preStop` drain (waits until the pod's `:1234` is idle)
  + long `terminationGracePeriodSeconds` + PDB, so a build is never killed
  mid-flight.
- **Node consolidation** — the NodePool uses Karpenter `consolidationPolicy:
  WhenEmpty`: a node is reclaimed only once it has no buildkitd pod, never by
  evicting a running build to bin-pack. So after a burst, scattered survivor
  pods can leave nodes half-full (more nodes than the cold baseline) until they
  drain naturally — a deliberate trade of some idle node cost for zero build
  disruption.

Build clients should retry the connect so a build can wait for a pod from a cold
or queued pool.

Requires the `keda` module deployed before `buildkit` (provides the CRDs).
