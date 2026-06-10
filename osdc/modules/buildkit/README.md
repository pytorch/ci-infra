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

## Build with `buildctl`, not `docker buildx`

Clients must reach the pool with **`buildctl`** (`buildctl --addr
tcp://buildkitd-<arch>.buildkit:1234 build ...`), not `docker buildx` against a
remote builder.

The autoscaling design relies on a *patient* client: during a burst the build's
connection sits in HAProxy's queue (above) for the minutes it takes KEDA +
Karpenter to add a pod, and that pending connection is also what keeps the
scale-up signal alive. `buildctl` does exactly this — its build call waits in
the queue up to `timeout queue` with no separate connect deadline.

`docker buildx` does **not**: before solving it "boots" the remote builder with
a **hardcoded ~20s connect timeout** (`[internal] waiting for connection`), which
is not configurable and far shorter than a cold scale-up. Under a burst the
connection is still queued at 20s, buildx aborts with `context deadline
exceeded`, and — because the connection then drops — the scale-up signal
disappears before KEDA can act, so the pool never grows and every queued build
fails. (`docker/setup-buildx-action` hits the same gate via `inspect
--bootstrap`; removing it doesn't help because `docker buildx build` re-runs the
same boot.) This was confirmed on the staging cluster. So PyTorch's
`.ci/docker/build.sh` uses `buildctl` whenever `REMOTE_BUILDKIT` is set.

## HAProxy config changes roll the LB

HAProxy renders its config only at container start, and nothing else restarts
the `buildkitd-lb` pod, so a bare ConfigMap update (`maxconn`, timeouts,
backends) would silently not take effect. `deploy.sh` stamps the LB pod template
with a `checksum/config` annotation = a hash of `haproxy.yaml`; when the config
changes the hash changes, which rolls the Deployment so the new pod picks up the
new config. An unchanged config keeps the same hash, so routine deploys don't
churn the LB. (The buildkitd worker pods do **not** yet have this, so a
`buildkitd.toml` / `drain.sh` change needs a manual rollout to take effect.)

Requires the `keda` module deployed before `buildkit` (provides the CRDs).
