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
  `max-parallelism = 1`). Excess builds **queue** in HAProxy (`timeout queue`)
  instead of stacking on a busy pod; as new pods register (DNS), queued builds
  flow onto them, so scaled-up pods never sit idle.
- **In-cluster scale signal** — KEDA `ScaledObject` per arch, `metrics-api`
  scraping the LB's own metrics (`haproxy_backend_current_sessions`) — no external
  metrics backend.
- **Warm baseline** — `amd64_min` / `arm64_min` keep ≥1 node per arch up so the
  common case gets a free warm pod immediately. `*_max` caps the burst; NodePool
  limits are sized to `*_max`.
- **Safe scale-down** — `preStop` drain (waits until the pod's `:1234` is idle)
  + long `terminationGracePeriodSeconds` + PDB, so a build is never killed
  mid-flight. Scale-down removes an arbitrary pod, which may be mid-build; the
  drain holds termination until that build finishes, but
  `terminationGracePeriodSeconds` is a hard SIGKILL cap, so it must outlast the
  longest possible build. It's set to **8100s (135m) = 120m** (the max time a
  docker build may run, matching HAProxy `timeout server`) **+ ~15m** of
  headroom for the drain's idle-detection polling. A build that starts just
  before drain still completes; the cap only fires as a backstop if a pod never
  drains.
  The **PDB** (`maxUnavailable: 1` per arch) bounds *voluntary* disruptions —
  node consolidation and manual `kubectl drain` — to one builder per arch at a
  time, so those go through the preStop drain one pod at a time instead of
  evicting several in-flight builds at once. (KEDA scale-down deletes pods
  directly rather than via the eviction API, so it isn't PDB-gated — the drain +
  grace cap above is what protects that path.)

Build clients should retry the connect so a build can wait for a pod from a cold
or queued pool.

Requires the `keda` module deployed before `buildkit` (provides the CRDs).
