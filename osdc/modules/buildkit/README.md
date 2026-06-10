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
  `max-parallelism = 1`) so a build never stacks on a busy pod. When every pod is
  busy the LB has no slot, so the client must **retry the connect** (see below)
  until a pod frees or the pool scales up.
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

## Clients must retry the connect

Build clients (both `docker buildx` and `buildctl`) use the `moby/buildkit` Go
client, which dials with gRPC's default **~20s `MinConnectTimeout`** and
**fail-fast** RPCs — there is no client-side flag to make it wait longer. During
a burst, a build whose connection finds no free pod (`maxconn 1`) is dropped by
the client after ~20s, well before KEDA/Karpenter can add a pod (minutes). An
HAProxy-side `timeout queue` does **not** help: the client gives up at 20s
regardless, so queueing on the LB is pointless (and was removed).

So the **client must retry the build** on connection failures until a pod is
free or the pool has scaled up; the repeated attempts also keep the autoscaler's
load signal alive. PyTorch's `.ci/docker/build.sh` does this when
`REMOTE_BUILDKIT` is set, and the workflow creates the remote builder *without*
`--bootstrap` (the `docker buildx inspect --bootstrap` health check hits the same
20s gate at setup). This was confirmed on the staging cluster.

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
