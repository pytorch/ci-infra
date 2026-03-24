# Runner Load Distribution (pytorch/pytorch)

Job counts and peak concurrency by runner type over the last 30 days. Source: `default.workflow_job` ClickHouse table, queried 2026-03-18.

**Methodology:**
- Job counts: `COUNT(*)` per runner label.
- Peak concurrency: event-based calculation using `started_at` / `completed_at` timestamps. Each job creates a +1 event at start and -1 at completion; a running sum gives the concurrent count at every event; the max is the peak. Long-running jobs (hours) are properly counted for their full duration.
- The `lf.` prefix (Linux Foundation) is stripped and merged with non-prefixed equivalents — they run on the same hardware.
- Meta-labels (`self-hosted`, `Linux`, `macOS`, `windows`, `X64`, `ARM64`, etc.) are excluded.
- **Scope: pytorch/pytorch only.** Other repos share the same runner pools but are not included. True infrastructure peak may be higher.

## Self-hosted Linux runners (old labels)

| Runner Type | Jobs (30d) | Peak Concurrent |
|---|---|---|
| linux.2xlarge | 491,396 | 1,473 |
| linux.c7i.2xlarge | 350,911 | 927 |
| linux.4xlarge | 279,660 | 1,293 |
| linux.g5.4xlarge.nvidia.gpu | 93,759 | 695 |
| linux.g6.4xlarge.experimental.nvidia.gpu | 61,263 | 422 |
| linux.2xlarge.amx | 46,174 | 384 |
| linux.large | 37,043 | 15 |
| linux.12xlarge | 31,542 | 164 |
| linux.c7i.4xlarge | 29,111 | 91 |
| linux.arm64.m7g.4xlarge | 23,594 | 76 |
| linux.g4dn.metal.nvidia.gpu | 19,040 | 91 |
| linux.arm64.m8g.4xlarge | 17,935 | 76 |
| linux.g4dn.12xlarge.nvidia.gpu | 17,625 | 183 |
| linux.12xlarge.memory | 10,978 | 64 |
| linux.4xlarge.memory | 10,695 | 71 |
| linux.12xlarge.memory.ephemeral | 9,815 | 353 |
| linux.g5.12xlarge.nvidia.gpu | 7,932 | 80 |
| linux.9xlarge.ephemeral | 7,678 | 65 |
| linux.8xlarge.amx | 7,663 | 174 |
| linux.24xl.spr-metal | 6,983 | 45 |
| linux.r7i.2xlarge | 5,173 | 24 |
| linux.arm64.r7g.12xlarge.memory | 5,046 | 153 |
| linux.arm64.2xlarge | 4,552 | 53 |
| linux.24xlarge.memory | 3,652 | 28 |
| linux.g4dn.4xlarge.nvidia.gpu | 3,651 | 83 |
| linux.g5.48xlarge.nvidia.gpu | 3,465 | 42 |
| linux.g6.12xlarge.nvidia.gpu | 3,261 | 29 |
| linux.arm64.2xlarge.ephemeral | 1,536 | 20 |
| linux.8xlarge.memory | 1,522 | 12 |
| linux.r7i.4xlarge | 1,449 | 18 |
| linux.10xlarge.avx2 | 1,290 | 30 |
| linux.arm64.m7g.metal | 1,170 | 39 |
| linux.c7i.12xlarge | 858 | 25 |
| linux.24xlarge.amd | 720 | 24 |
| linux.2xlarge.avx2 | 534 | 18 |
| linux.4xlarge.nvidia.gpu | 468 | 21 |
| linux.2xlarge.memory | 15 | 4 |
| linux.24xlarge | 14 | 2 |

## GitHub-hosted runners

| Runner Type | Jobs (30d) | Peak Concurrent |
|---|---|---|
| ubuntu-latest | 276,308 | 137 |
| linux.24_04.4x | 58,699 | 98 |
| ubuntu-22.04 | 56,849 | 77 |
| ubuntu-24.04 | 1,611 | 4 |

## Windows runners

| Runner Type | Jobs (30d) | Peak Concurrent |
|---|---|---|
| windows.4xlarge.nonephemeral | 33,998 | 227 |
| windows.12xlarge | 5,480 | 190 |
| windows.g4dn.xlarge | 3,397 | 49 |
| windows.4xlarge | 2,949 | 49 |
| windows-11-arm64-preview | 1,412 | 16 |

## macOS runners

| Runner Type | Jobs (30d) | Peak Concurrent |
|---|---|---|
| macos-m1-stable | 23,884 | 90 |
| macos-m2-15 | 4,973 | 34 |
| macos-m1-14 | 4,789 | 30 |
| macos-14-xlarge | 1,067 | 21 |
| macos-m2-26 | 179 | 4 |

## ROCm (AMD GPU) runners

| Runner Type | Jobs (30d) | Peak Concurrent |
|---|---|---|
| linux.rocm.gpu.gfx950.1 | 41,952 | 323 |
| linux.rocm.gpu.gfx950.4 | 13,150 | 122 |
| linux.rocm.gpu.gfx942.1 | 7,913 | 80 |
| linux.rocm.gpu.gfx950.2 | 2,894 | 99 |
| linux.rocm.gpu.mi210.1.test | 1,935 | 37 |
| linux.rocm.gpu.mi250.1 | 1,264 | 70 |
| linux.rocm.gpu.gfx942.4 | 1,247 | 27 |
| linux.rocm.gpu.2 | 1,236 | 48 |
| linux.rocm.gpu.mi210.2.test | 725 | 16 |
| linux.rocm.gpu.gfx1100 | 638 | 10 |
| linux.rocm.gpu.gfx950.1.test | 285 | 48 |
| linux.rocm.gpu.mi300-test | 182 | 16 |
| linux.rocm.gpu.4 | 176 | 12 |
| linux.rocm.gpu.gfx950.4.test | 139 | 27 |
| linux.rocm.gpu.gfx942.1.stg | 100 | 8 |
| linux.rocm.mi250.docker-cache | 90 | 2 |
| linux.rocm.mi210.docker-cache | 89 | 2 |
| linux.rocm.gpu.gfx942.4.stg | 36 | 3 |
| linux.rocm.gpu.gfx942.1.b | 11 | 2 |
| linux.rocm.gpu.gfx942.1.test | 5 | 5 |
| linux.rocm.gpu.gfx942.4.b | 3 | 3 |
| linux.rocm.gpu.gfx942.4.test | 3 | 3 |

## Other providers (AWS H100, DGX B200, TPU, XPU, s390x)

| Runner Type | Jobs (30d) | Peak Concurrent |
|---|---|---|
| linux.idc.xpu | 5,721 | 70 |
| linux.aws.h100 | 4,940 | 44 |
| linux.dgx.b200 | 3,116 | 29 |
| linux.s390x | 2,283 | 45 |
| linux.aws.a100 | 1,803 | 38 |
| linux.google.tpuv7x.1 | 1,512 | 10 |
| linux.dgx.b200.8 | 445 | 12 |
| linux.aws.h100.4 | 244 | 9 |
| linux.client.xpu | 97 | 8 |
| linux.aws.h100.8 | 36 | 1 |
| a.linux.b200.2 | 9 | 2 |

## OSDC runners (new labels, early migration)

| Runner Type | Jobs (30d) | Peak Concurrent |
|---|---|---|
| l-x86iamx-8-16 | 89 | 15 |
| l-x86iavx512-2-4 | 18 | 3 |
| l-x86iavx512-16-64-t4 | 9 | 2 |
| l-x86iavx512-8-16 | 9 | 2 |

---

## Estimated Fleet Size at Peak (Simulation)

Estimated node count and resource usage when all runners hit their peak concurrency simultaneously. Generated by the OSDC cluster simulator (`just simulate-cluster`), which maps old runner labels to new OSDC labels (see `docs/runner_naming_convention.md`), then bin-packs runners onto nodes using actual runner and nodepool definitions from `modules/arc-runners/defs/` and `modules/nodepools/defs/`.

**Date:** 2026-03-24. **Scope:** pytorch/pytorch self-hosted Linux runners only (same scope as the load data above). Runner types with no old-label mapping (e.g., some AMX variants on c7i.12xlarge and r7i.48xlarge) are not included — actual fleet will be slightly larger.

### Nodes by instance type

| Instance Type | Nodes | vCPU Used | vCPU Total | Mem Used | Mem Total | GPU |
|---|---|---|---|---|---|---|
| c7a.48xlarge | 267 | 47,488c | 51,022c | 89,772Gi | 92,398Gi | — |
| c7i.metal-24xl | 36 | 3,351c | 3,432c | 6,048Gi | 6,065Gi | — |
| g4dn.12xlarge | 157 | 7,233c | 7,435c | 27,163Gi | 27,277Gi | 628 |
| g4dn.8xlarge | 89 | 2,676c | 2,794c | 10,325Gi | 10,362Gi | 89 |
| g4dn.metal | 79 | 7,511c | 7,524c | 27,256Gi | 27,329Gi | 632 |
| g5.12xlarge | 58 | 2,672c | 2,747c | 9,745Gi | 9,763Gi | 232 |
| g5.48xlarge | 40 | 7,603c | 7,640c | 28,200Gi | 28,214Gi | 320 |
| g5.8xlarge | 596 | 17,922c | 18,711c | 67,950Gi | 68,264Gi | 596 |
| g6.12xlarge | 26 | 1,198c | 1,231c | 4,498Gi | 4,517Gi | 104 |
| g6.8xlarge | 388 | 11,667c | 12,181c | 44,236Gi | 44,440Gi | 388 |
| m6i.32xlarge | 27 | 3,404c | 3,436c | 12,528Gi | 12,543Gi | — |
| m7i.48xlarge | 48 | 8,569c | 9,173c | 32,586Gi | 33,660Gi | — |
| m8g.16xlarge | 29 | 1,829c | 1,839c | 6,583Gi | 6,604Gi | — |
| m8g.48xlarge | 15 | 2,650c | 2,866c | 10,022Gi | 10,519Gi | — |
| r7a.48xlarge | 138 | 22,129c | 26,371c | 172,955Gi | 194,809Gi | — |
| r7g.16xlarge | 120 | 7,448c | 7,610c | 55,681Gi | 55,749Gi | — |
| **Total** | **2,113** | **155,350c** | **166,012c** | **605,548Gi** | **632,513Gi** | **2,989** |

### Cluster-wide utilization

| Resource | Usage |
|---|---|
| vCPU | 93.6% (155,350 / 166,012 cores) |
| Memory | 95.7% (605,548 / 632,513 GiB) |
| GPU | 100.0% (2,989 / 2,989 GPUs across 1,433 nodes) |

### Monte Carlo stability (200 rounds, seeds 42–241)

| Resource | Avg | p50 | p25 | Worst |
|---|---|---|---|---|
| vCPU | 93.5% | 93.5% | 93.4% | 93.0% |
| Memory | 95.6% | 95.6% | 95.4% | 94.7% |
| GPU | 100.0% | 100.0% | 100.0% | 100.0% |

Nodes: avg 2,105 — min 2,013 — max 2,201

### Runner deployment vs. peak targets

Deployment accuracy: 6,268 / 7,367 target runners deployed (weighted MAPE 15.0%). The gap is due to bin-packing waste — some node types can't perfectly fill with their target runner mix. Largest shortfalls are on the highest-volume small runners (l-x86iavx512-8-16: −350, l-x86iavx512-16-32: −230).

| Runner | Deployed | Target | Diff |
|---|---|---|---|
| l-x86iavx512-8-16 | 2,050 | 2,400 | −350 |
| l-x86iavx512-16-32 | 1,154 | 1,384 | −230 |
| l-x86aavx2-29-113-a10g | 596 | 695 | −99 |
| l-x86iavx512-48-384 | 364 | 417 | −53 |
| l-x86iamx-32-128 | 131 | 174 | −43 |
| l-x86iamx-8-32 | 345 | 384 | −39 |
| l-x86aavx2-29-113-l4 | 388 | 422 | −34 |
| l-arm64g3-61-463 | 120 | 153 | −33 |
| l-x86iavx512-46-85 | 160 | 189 | −29 |
| l-x86iavx512-37-68 | 38 | 65 | −27 |
| l-x86iavx512-45-172-t4-4 | 157 | 183 | −26 |
| l-x86aavx2-45-167-a10g-4 | 58 | 80 | −22 |
| l-x86iavx512-29-115-t4 | 89 | 104 | −15 |
| l-arm64g3-16-62 | 62 | 76 | −14 |
| l-arm64g2-6-32 | 61 | 73 | −12 |
| l-bx86iavx512-94-344-t4-8 | 79 | 91 | −12 |
| l-barm64g4-62-226 | 29 | 39 | −10 |
| l-bx86iamx-92-167 | 36 | 45 | −9 |
| l-x86iavx512-8-64 | 19 | 28 | −9 |
| l-arm64g4-16-62 | 68 | 76 | −8 |
| l-x86iavx2-40-160 | 23 | 30 | −7 |
| l-x86iavx512-16-128 | 83 | 89 | −6 |
| l-x86iavx512-2-4 | 9 | 15 | −6 |
| l-x86iavx512-94-768 | 24 | 28 | −4 |
| l-x86aavx2-45-172-l4-4 | 26 | 29 | −3 |
| l-x86aavx2-189-704-a10g-8 | 40 | 42 | −2 |
| l-x86iavx2-8-32 | 18 | 18 | 0 |
| l-x86iavx512-32-256 | 12 | 12 | 0 |
| l-x86iavx512-94-192 | 2 | 2 | 0 |
| l-x86aavx512-125-463 | 27 | 24 | +3 |

## How to reproduce and update these estimates

The fleet estimates above are derived from three `just` recipes. Re-run them whenever runner definitions, nodepool definitions, or load data change.

### Data sources

- **Peak concurrency targets**: from the "Self-hosted Linux runners" table above (Peak Concurrent column), mapped through the old→new label table in `docs/runner_naming_convention.md`
- **Runner definitions**: `modules/arc-runners/defs/` (vCPU, memory, GPU requests per runner type)
- **NodePool definitions**: `modules/nodepools/defs/` (instance types, allocatable resources, taints)

### Commands

```bash
# Per-node-type packing efficiency: how many runners fit on each node,
# CPU/memory waste, best/worst mixed combos, DaemonSet overhead
just analyze-utilization

# Single simulation run: maps old labels to new runners, bin-packs onto
# nodes, reports fleet size + per-runner deployment accuracy
just simulate-cluster

# Monte Carlo stability check: runs N rounds with different random seeds,
# reports avg/p50/p25/worst utilization and node count range
just simulate-cluster --rounds 200
```

### When to re-run

- After adding, removing, or resizing runner definitions in `modules/arc-runners/defs/`
- After changing nodepool instance types in `modules/nodepools/defs/`
- After updating the peak concurrency data in the load distribution tables above
- After changing DaemonSet resource requests (affects per-node overhead)

---

## Estimated Node Churn

How many nodes are created and destroyed daily as load fluctuates. This is a rough model — use it for capacity planning and cost estimation, not as a precise forecast.

**Date:** 2026-03-24.

### Data sources

- **Job submission rates**: [Grafana public dashboard](https://pytorchci.grafana.net/public-dashboards/9b2a3557de854d79a55f3a08bffcdec7) for pytorch/pytorch, observed weekly pattern
- **Peak fleet**: 2,113 nodes / 7,367 target runners from `just simulate-cluster` (see fleet estimation section above)

### Observed job submission pattern (pytorch/pytorch)

| Period | Jobs/hour |
|---|---|
| Weekday peak (10–11am ET) | ~8,500 |
| Weekday trough (2–3am ET) | ~2,400 |
| Weekend sustained | ~1,000 |

### Estimation method

1. **Derive average job duration** from peak concurrency and peak throughput:

   ```
   avg_duration ≈ peak_concurrent_runners / peak_jobs_per_hour
                = 7,367 / 8,500
                ≈ 0.87 hours ≈ 52 minutes
   ```

2. **Estimate concurrent runners at each load level** using Little's Law:

   ```
   concurrent_runners ≈ jobs_per_hour × avg_duration_in_hours
   ```

3. **Scale node count proportionally** to the ratio of concurrent runners vs. peak:

   ```
   nodes ≈ peak_nodes × (jobs_per_hour / peak_jobs_per_hour)
   ```

### Fleet size by time of day

| Period | Jobs/hr | Ratio to peak | Est. Concurrent Runners | Est. Nodes |
|---|---|---|---|---|
| Weekday peak (10–11am) | 8,500 | 1.00 | ~7,367 | ~2,113 |
| Weekday trough (2–3am) | 2,400 | 0.28 | ~2,081 | ~597 |
| Weekend sustained | 1,000 | 0.12 | ~867 | ~249 |

### Daily node churn

| Transition | Direction | Nodes | Approx. duration |
|---|---|---|---|
| Weekday ramp-up (3am → 11am) | create | ~1,516 | ~6–8 hours (~250 nodes/hr) |
| Weekday drain (11am → 3am) | destroy | ~1,516 | ~6–8 hours (~250 nodes/hr) |
| Friday night → Saturday | destroy | ~348 | gradual overnight |
| Sunday night → Monday ramp | create | ~1,864 | ~6–8 hours |

**Summary**: ~3,000 node lifecycle events per weekday (create + destroy), ~17,000 per week.

### Assumptions and caveats

1. **Uniform job duration**: the model assumes 52-minute average duration is constant across time of day. In practice, nighttime and weekend jobs likely skew toward longer-running GPU tests. This would mean trough fleet is *larger* than estimated (more concurrent runners per job/hour), reducing actual daily churn to perhaps ~2,000–2,500 events.

2. **Sum-of-peaks vs. simultaneous peak**: the 7,367 target runners is the sum of each runner type's individual peak concurrency. These peaks don't all occur at the same instant, so the true simultaneous peak is lower. This means the 52-minute duration estimate is likely an *underestimate* — real average duration may be 60–90 minutes, which again pushes trough fleet up and churn down.

3. **Linear scaling**: the model assumes node count scales linearly with job volume. In practice, bin-packing efficiency varies with job mix — a cluster full of small 2-vCPU runners packs differently than one full of 94-vCPU bare-metal runners. If the job mix shifts overnight (e.g., fewer small lint jobs, proportionally more large GPU jobs), node count doesn't drop as steeply as throughput.

4. **Karpenter hysteresis**: real node removal isn't instant. Karpenter's consolidation policy and the node compactor's anti-flap controls add delays between load drop and node termination. This smooths the churn curve — fewer sharp spikes, more gradual ramps.

5. **Scope**: pytorch/pytorch only. Other repos sharing the runner pools add load not captured here.
