# DCGM Exporter Memory Tuning (OSDC)

## Context

The `dcgm-exporter` DaemonSet (`modules/monitoring/kubernetes/dcgm-exporter/`) was
OOMKilled fleet-wide (`osdc#1011`). The kills bucket cleanly by **GPU count**: ~48% of
the 8-GPU dcgm pods were OOMKilled, versus **0%** on 1-GPU and 4-GPU nodes. The issue
gives no per-instance-type breakdown — only this GPU-count split. The exporter is
deliberately sized tight: `requests=256Mi / limits=512Mi`, `GOMEMLIMIT=410MiB`, image
`nvcr.io/nvidia/k8s/dcgm-exporter:4.5.2-4.8.1-distroless`, `--collect-interval=60000`
(60s), and a curated 23-metric subset. `GOMAXPROCS` was unset.

The steady-state working set sat at 91-95% of the 512Mi limit on **every** tier
(1-, 4-, and 8-GPU), so flat metric sampling showed all tiers equally close to the
ceiling and none obviously worse. The killer was therefore a **transient peak** that
periodic sampling missed, occurring only on the 8-GPU tier — not a steady-state
regression that a bigger limit or a lower `GOMEMLIMIT` would have caught.

## TL;DR

- dcgm-exporter OOMKilled only on 8-GPU nodes (`osdc#1011`): ~48% of 8-GPU pods, 0% on
  1-GPU/4-GPU. The issue buckets by GPU count and gives no per-instance-type data.
- Two mechanisms are both consistent with an 8-GPU-only pattern and cannot be separated
  from the aggregate alone: **(1)** the pinned Go 1.24 image has no cgroup-aware
  `GOMAXPROCS` (a Go 1.25 feature), so `GOMAXPROCS` defaults to the host vCPU count —
  up to 192 on the largest 8-GPU nodes — scaling runtime concurrency; **(2)** native
  `libdcgm` per-GPU buffers scale with GPU count. `GOMEMLIMIT` is a *soft*, CPU-capped
  limit, so a highly concurrent per-scrape allocation burst can transiently overshoot
  it; that spike plus native RSS breaches the *hard* 512Mi cgroup ceiling.
- Fix: pin `GOMAXPROCS=2` (the value Go 1.25's cgroup-aware default computes for the
  200m CPU limit), with **no** memory-limit increase. It is the right first lever
  regardless of which mechanism dominates: one line, zero memory, removes mechanism (1)
  and dampens the transient peak.
- The pin becomes redundant (but harmless) once the image is bumped to a Go 1.25+
  build; exporter `4.6.0-4.8.3` already ships on Go 1.26.
- If OOMs persist after rollout, mechanism (2) dominates — the next zero-memory lever is
  trimming the 23-metric CSV. Do **not** lower `GOMEMLIMIT` further.

## Root cause

### What osdc#1011 shows

The issue reports the OOMs bucketed by GPU count only: 8-GPU pods ~48% OOMKilled,
1-GPU and 4-GPU pods 0%. There is **no** per-instance-type breakdown in the aggregate,
so any claim finer than "8-GPU nodes OOM, smaller ones do not" is a hypothesis, not a
measured fact.

### Two candidate mechanisms

Within the 8-GPU tier, GPU count is fixed at 8, but vCPU count varies: `p4d.24xlarge`
and `g4dn.metal` have 96 vCPU, while `p5.48xlarge`, `g5.48xlarge`, `g6.48xlarge`, and
`p6-b200.48xlarge` have 192. (1-GPU nodes have 32-64 vCPU; 4-GPU nodes 48-96.) Two
distinct mechanisms scale with these axes, and **both** are consistent with
"only 8-GPU nodes OOM":

1. **vCPU-scaled runtime concurrency.** The upstream module declares `go 1.24.0` /
   `toolchain go1.24.13` in its `go.mod`, imports no `automaxprocs` shim, and sets no
   explicit `GOMAXPROCS`. Container-aware `GOMAXPROCS` only landed in Go 1.25, so on
   Go 1.24 `GOMAXPROCS` defaults to `runtime.NumCPU()` — the **host** vCPU count,
   ignoring the pod's `200m` CPU limit. On a 192-vCPU node the exporter runs with
   `GOMAXPROCS=192`, so a per-scrape allocation burst can run up to 192-way concurrent.
2. **GPU-count-scaled native memory.** `libdcgm` allocates per-GPU buffers, so an
   8-GPU node holds roughly 8× the per-GPU footprint of a 1-GPU node. This is native
   (cgo) memory.

### Why GOMEMLIMIT=410MiB didn't prevent it

`GOMEMLIMIT` is a **soft** limit that accounts for essentially **all**
Go-runtime-managed memory: the live heap, goroutine stacks, per-`P` mcaches, and
runtime metadata (in effect, runtime `Sys` minus pages already returned to the OS). It
excludes only native/cgo allocations (`libdcgm`) and OS-reclaimed pages. So the per-`P`
caches that grow with `GOMAXPROCS` **are** counted by `GOMEMLIMIT` — an earlier framing
that called them "off-heap and therefore outside the limit" was wrong.

The limit fails for a different reason: it is *soft*, and GC is *CPU-capped*. The
runtime bounds GC assist + background work to roughly 50% of CPU over a sliding
`2×GOMAXPROCS` window, so with `GOMAXPROCS=192` a scrape's up-to-192-way-concurrent
allocation burst transiently **overshoots** the 410MiB soft target faster than the
CPU-capped GC can rein it back. That transient spike, plus native `libdcgm` RSS (the
one part `GOMEMLIMIT` does **not** count), breaches the **hard** 512Mi cgroup ceiling —
even though periodic sampling only ever showed the 91-95% steady state. Pinning
`GOMAXPROCS=2` caps the burst concurrency so the overshoot is small and GC keeps pace.

This is also why the two prior `GOMEMLIMIT` reductions (`osdc#631`, `osdc#934`) did not
help: `GOMEMLIMIT` was never the binding constraint. The binding constraints are the
hard cgroup limit, the native memory the limit does not count, and the transient
overshoot — none of which a lower soft target addresses; a lower target mostly just
makes GC thrash.

### Which mechanism dominates is unresolved

Both mechanisms fit the 8-GPU-only pattern, and the aggregate in `osdc#1011` cannot
separate them. Disambiguating needs **per-instance-type** OOM data — specifically
comparing 96-vCPU (`p4d.24xlarge`, `g4dn.metal`) against 192-vCPU
(`p5`/`g5.48xlarge`/`g6.48xlarge`/`p6-b200`) 8-GPU nodes. If OOMs concentrate on the
192-vCPU nodes, mechanism (1) dominates; if they are uniform across the 8-GPU tier
regardless of vCPU, mechanism (2) does. The prod soak (below) produces exactly that
breakdown.

## The fix

Pin `GOMAXPROCS=2` in the DaemonSet env, with **no** memory-limit increase.

`2` is exactly what Go 1.25's cgroup-aware default would compute for the pod's `200m`
CPU limit: `ceil(0.2) = 1`, floored to the runtime's hard minimum of `2`. Pinning it on
the Go 1.24 image reproduces the behavior the runtime will adopt on its own once the
image is upgraded. It is a one-line, zero-memory change that removes mechanism (1):
capping concurrency stops the per-scrape burst from running 192-way and lets GC keep
pace, shrinking the transient overshoot.

A dcgm-shaped Go microbenchmark (`GOMEMLIMIT=410`, ~50 MiB live heap, concurrent
collectors) measured peak RSS against `GOMAXPROCS`:

| `GOMAXPROCS` | Peak RSS |
|--------------|----------|
| 192          | 261 MiB  |
| 96           | 246 MiB  |
| 12           | 161 MiB  |
| 2            | 65 MiB   |

Read this as **direction and mechanism only** — peak RSS falls sharply as `GOMAXPROCS`
drops, confirming that runtime concurrency drives the transient. It is **not** a
production magnitude: the benchmark's 261 MiB peak is well below real dcgm-exporter RSS
(451-486 MiB per `osdc#1011`), so the ~196 MiB benchmark delta must **not** be read as
production headroom. The actual production reduction is unmeasured; the prod soak will
quantify it.

The pin is safe for collection throughput: the exporter's blocking cgo calls into
`libdcgm` **detach their `P`** while blocked, so scrapes are not serialized behind two
runnable goroutines. At a 60s collection interval under a `200m` CPU cap, running with
`GOMAXPROCS=2` also reduces CFS throttling relative to a runtime that spins up 192
worker threads for a workload the scheduler only funds for 0.2 of a core.

## Caveat and next lever

`GOMAXPROCS` does **not** touch the native `libdcgm` per-GPU memory (mechanism 2). If
8-GPU nodes still OOM after this rollout — especially uniformly across the 96- and
192-vCPU types — mechanism (2) dominates, and the next lever that needs no memory bump
is trimming the 23-metric `custom-metrics.csv`: only ~4 of the 23 metrics have in-repo
alert consumers. Note that the ServiceMonitor `metricRelabelings` label-drops
(`UUID`, `modelName`, etc.) are **scrape-side** — they shrink what Mimir stores, not
what the exporter holds in RSS, so they are not a memory lever.

Do **not** lower `GOMEMLIMIT` further. It was never the binding constraint (the hard
cgroup limit, the native memory it does not count, and the transient overshoot are);
lowering the soft target risks GC thrash and touches neither the native memory nor the
spike.

The `GOMAXPROCS=2` pin becomes **redundant but harmless** once the image is bumped to a
Go 1.25+ build, where the cgroup-aware default computes the same `2` on its own.
Exporter `4.6.0-4.8.3` already ships on Go 1.26.

## Validation

This is a **prod-soak-only** change — staging has no GPU nodes, so the fix cannot be
exercised before merge. After rollout, watch over several days, broken out **per
instance type**:

- `container_memory_working_set_bytes` for the dcgm-exporter pods — the peak should
  drop below the 512Mi limit.
- OOM attribution: `kube_pod_container_status_last_terminated_reason="OOMKilled"`
  joined to `kube_pod_info` (`on(namespace, pod) group_left(node)`) to attribute kills
  per node and instance type. This is also the data that separates mechanism (1) from
  (2) — compare 96-vCPU against 192-vCPU 8-GPU nodes.

The `test_dcgm_exporter_no_crashloop` smoke test
(`modules/monitoring/tests/smoke/test_monitoring.py`) cannot validate this fix
pre-merge for two reasons: **(a)** it `pytest.skip`s entirely when no dcgm-exporter pods
exist, which is the case on staging (no GPU nodes); and **(b)** it keys on
point-in-time `restartCount` / `CrashLoopBackOff` state, which — per its own docstring —
can miss OOMs when Karpenter terminates the node before the next reconcile. (It does
fail at `>= 3` restarting nodes, so it catches a *sustained* fleet-wide crashloop; it
just cannot serve as the pre-merge check for this change.)

## Key file pointers

- `modules/monitoring/kubernetes/dcgm-exporter/daemonset.yaml` — `GOMAXPROCS` /
  `GOMEMLIMIT` env and the `256Mi/512Mi` resource shape.
- `modules/monitoring/kubernetes/dcgm-exporter/custom-metrics-configmap.yaml` — the
  23-metric subset; the next lever if a node still hugs the limit.
- `modules/monitoring/kubernetes/monitors/dcgm-servicemonitor.yaml` — scrape-side
  label-drops (do not reduce exporter RSS).
- `modules/monitoring/tests/smoke/test_monitoring.py` — `test_dcgm_exporter_no_crashloop`
  (skips without GPU nodes; keys on point-in-time restart state).

## References

- OSDC: `osdc#1011` (this OOM), `osdc#631` and `osdc#934` (prior `GOMEMLIMIT`
  reductions that did not resolve it).
- [Container-aware `GOMAXPROCS`](https://go.dev/blog/container-aware-gomaxprocs) and the
  [Go 1.25 release notes](https://go.dev/doc/go1.25) — the runtime change that makes the
  pin redundant once the image is upgraded.
- [Go GC guide](https://go.dev/doc/gc-guide) — `GOMEMLIMIT` soft-limit semantics (what
  the limit does and does not account for).
- NVIDIA dcgm-exporter upstream issues
  [#425](https://github.com/NVIDIA/dcgm-exporter/issues/425) and
  [#536](https://github.com/NVIDIA/dcgm-exporter/issues/536).
