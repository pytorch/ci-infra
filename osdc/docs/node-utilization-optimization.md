# Node Utilization Optimization

Analysis of runner-to-node packing efficiency across all OSDC nodepools, with recommendations for instance type changes to reduce waste.

> **Snapshot status**: This is a Phase 1 + Phase 2 right-sizing retrospective from the time of commit `fcc4df1`. The recommendations were applied. Several things have changed since: nodepool defs were migrated to fleet format (`fa950b4`), A100 (`93a2d66`) and H100 (`ed01267`) GPU nodepools were added, release runners (`903d1d4`) and the dedicated `c7i-runner` pool now exist, and tunables like `r7g.16xlarge` `node_disk_size` have been retuned. See the inline notes for specifics.
>
> **Pricing disclaimer**: Dollar figures throughout this doc are point-in-time AWS list prices captured during the Phase 2 analysis. They may have drifted with AWS pricing changes, region differences, RI/Savings Plan coverage, and Capacity Block pricing. Use them as **relative** indicators of cost differences between instance types — re-check current AWS pricing before basing budget decisions on the absolute numbers.

## Problem Statement

OSDC uses Kubernetes Guaranteed QoS pods (requests == limits) for all runner workloads. Each runner type has fixed CPU and memory requirements, and each nodepool uses a single AWS instance type. When runners with different CPU:memory ratios share a nodepool, the mismatched resource is wasted.

**Original state** (before Phase 2): 36 of 39 runner types had homogeneous utilization below 90%. Several had worst-case utilization under 20%. After Phase 2 (ratio-matched nodepools), x86 CPU worst-case improved from 12.6% to ~74% and ARM64 from 37.9% to 75.3%.

> **Note**: This analysis covers all runners including B200 runners in `modules/arc-runners-b200/defs/`.

## How Packing Works

Each node's allocatable resources are reduced by:

1. **Kubelet reserved CPU**: 60m (core 1) + 10m (core 2) + 5m/core (cores 3-4) + 2.5m/core (cores 5+)
2. **Kubelet reserved memory**: 255Mi + 11Mi per max_pod (ENI-based) + 100Mi eviction threshold
3. **DaemonSet overhead**: 460m CPU / 1158Mi memory (non-GPU), 560m CPU / 1414Mi memory (GPU nodes). Includes NodeLocal DNSCache (NLD) at 25m CPU / 100Mi memory per node, deployed on every node via DaemonSet.

Each workflow pod also includes runner-container-hooks containers (320m CPU + 522Mi memory) on top of the job container's resources. The runner-orchestrator pod itself (750m CPU / 1Gi memory) lives on the dedicated `c7i-runner` NodePool and does not consume resources on the workflow node — see `c7i-runner` notes below.

**Utilization** = `(pods_per_node * pod_resource) / allocatable_resource` for each dimension. The **minimum** across CPU and memory determines the packing efficiency — unused resources in the higher dimension are waste.

## Root Cause: CPU:Memory Ratio Mismatch

Runners fall into three ratio categories:

| Category | GiB/core | Example Runners | Best Instance Family |
|----------|----------|----------------|---------------------|
| Compute-heavy | ~2 GiB/core | 46c/85Gi, 94c/192Gi, 8c/16Gi | c-series (c7i, c7a) |
| Balanced | ~4 GiB/core | 8c/32Gi, 32c/128Gi, 40c/160Gi | m-series (m7i, m7a, m8g) |
| Memory-heavy | ~8 GiB/core | 8c/64Gi, 16c/128Gi, 48c/384Gi | r-series (r7i, r7a, r7g) |

**Original assignment** (before Phase 2): All CPU runners shared one r5.24xlarge (96c/768Gi, ratio 8:1). This was perfect for memory-heavy runners but wasted ~75% of memory for compute-heavy runners like `l-x86iavx512-46-85` (46c/85Gi, ratio 2:1). Phase 2 split these into ratio-matched nodepools (c/m/r series per ISA group).

## ISA Constraint Groups

Runners have ISA (instruction set) requirements encoded in their names that constrain which instance types they can use:

| ISA Tag | Instruction Set | Compatible Instance Families |
|---------|----------------|------------------------------|
| `iamx` | Intel AMX (Advanced Matrix Extensions) | Intel 4th gen+ only: c7i, m7i, r7i |
| `iavx512` | Intel AVX-512 | Intel: c6i, c7i, m6i, m7i, r5, r6i, r7i |
| `iavx2` / `aavx2` | AVX2 | Any x86: Intel (c/m/r 5/6i/7i) or AMD (c/m/r 6a/7a) |
| `arm64g3` / `arm64g4` | ARM (Graviton) | Graviton: m7g, m8g, r7g, r8g |

**Critical constraint**: AMX runners (`l-x86iamx-*`) require Intel Sapphire Rapids or newer. They **cannot run on AMD instances** (c6a, m6a, r6a, c7a, m7a, r7a). This limits instance selection for the "AVX2 group" which includes AMX runners.

## Pre-Optimization State by Nodepool

### Non-GPU Nodepools

| Nodepool | Instance | Runners | Worst Util | Problem |
|----------|----------|---------|-----------|---------|
| r5.24xlarge | 96c/768Gi @ $6.05/hr | 22 (amx/avx2/avx512) | **12.6%** | Compute-heavy runners waste 75%+ memory |
| r7g.16xlarge | 64c/512Gi @ $3.43/hr | 5 (arm64) | **37.9%** | Balanced runners on memory-optimized nodes |

### GPU Nodepools

| Nodepool | Instance | Runners | Worst Util | Problem |
|----------|----------|---------|-----------|---------|
| g5.48xlarge | 192c/768Gi @ $16.29/hr | 3 (A10G) | **50.3%** | 4-GPU runner uses only half the node |
| g6.48xlarge | 192c/768Gi @ $13.35/hr | 2 (L4) | **50.3%** | Same — 4-GPU runner on 8-GPU node |
| g4dn.8xlarge | 32c/128Gi @ $2.18/hr | 1 (T4) | **90.6%** | `l-x86iavx512-29-115-t4` fills the node |
| g4dn.12xlarge | 48c/192Gi @ $3.91/hr | 1 (T4) | **95.6%** | `l-x86iavx512-45-172-t4-4` fills the node (FIXED) |
| g4dn.metal | 96c/384Gi @ $7.82/hr | 1 (T4) | 96.0% | Good — `l-bx86iavx512-94-344-t4-8` fills the node |
| p6-b200.48xlarge | 192c/2048Gi @ $55.47/hr | 4 (B200) | 88.1% | Good — runners scale proportionally to GPU count |
| p4d.24xlarge | 96c/1152Gi (8x A100 40GB SXM4) | 4 (A100) | n/a | Added after this snapshot — `l-x86iavx512-{11-125,22-250,44-500}-a100*` + `l-bx86iavx512-88-1000-a100-8`, 1/2/4/8 GPU splits |
| p5.48xlarge | 192c/2048Gi (8x H100) | 4 (H100) | n/a | Added after this snapshot — `l-x86iamx-{22-225,44-450,88-900}-h100*` + `l-bx86iamx-176-1800-h100-8`, 1/2/4/8 GPU splits |

### Critical Issue: T4 Runner Cannot Schedule (RESOLVED)

The original 4-GPU T4 runner (`l-x86iavx512-48-192-t4-4`) requested 48 vCPU + 192Gi memory + 4 GPUs. After adding the 750m sidecar (total: 48750m CPU), it exceeded the g4dn.12xlarge allocatable CPU of 47465m and could never schedule.

**Fix applied**: Renamed to `l-x86iavx512-45-172-t4-4` with reduced vCPU (45) and memory (172Gi) to fit within g4dn.12xlarge allocatable resources. The 1-GPU T4 runner (`l-x86iavx512-29-115-t4`) runs on its own g4dn.8xlarge nodepool.

## Recommendations

### Recommendation 1: Split x86 CPU Nodepools by Ratio (HIGH IMPACT)

**Current**: 1 nodepool (r5.24xlarge) for all 22 x86 CPU runners → worst utilization 12.6%

**Proposed**: Split into 3 nodepools per ISA group, matched to CPU:memory ratio.

#### AMX/AVX2 Group (11 runners → 3 nodepools)

These runners include `l-x86iamx-*` (AMX) and `l-x86iavx2-*` (AVX2). Since AMX runners require Intel, **all pools in this group must use Intel instances** (c7i, m7i, r7i — not AMD c7a, m7a, r7a).

| Pool | Instance | Runners | Worst Util | Cost/hr |
|------|----------|---------|-----------|---------|
| Compute | **c7i.12xlarge** (48c/96Gi) + **c7i.metal-24xl** (96c/192Gi) | 4 shared + 1 bare-metal (2:1 ratio) | ~92-99% | $2.14 / $4.28 |
| Balanced | **m7i.48xlarge** (192c/768Gi) | 4 runners (4:1 ratio) | ~84% | $9.68 |
| Memory | **r7i.48xlarge** (192c/1536Gi) | 2 runners (8:1 ratio) | ~88% | $12.70 |

**Runner assignments**:
- Compute pool: `l-x86iamx-8-16`, `l-x86iamx-14-27`, `l-x86iamx-22-41`, `l-x86iamx-46-84` (c7i.12xlarge), `l-bx86iamx-92-167` (c7i.metal-24xl)
- Balanced pool: `l-x86iamx-8-32`, `l-x86iamx-32-128`, `l-x86iavx2-8-32`, `l-x86iavx2-40-160`
- Memory pool: `l-x86iamx-8-64`, `l-x86iamx-16-128`

**Improvement**: worst-case utilization 12.6% → ~74% (+61pp)

#### AVX-512 Group (12 runners → 3 nodepools)

AVX-512 runners can use either Intel or AMD instances with AVX-512 support.

| Pool | Instance | Runners | Worst Util | Cost/hr |
|------|----------|---------|-----------|---------|
| Compute | **c7a.48xlarge** (192c/384Gi) | 6 runners (2:1 ratio) | 76.0% | $8.24 |
| Balanced | **m6i.32xlarge** (128c/512Gi) | 1 runner (4:1 ratio) | 74.4% | $6.14 |
| Memory | **r7a.48xlarge** (192c/1536Gi) | 5 runners (8:1 ratio) | 75.2% | $12.21 |

**Runner assignments**:
- Compute pool: `l-x86iavx512-2-4`, `l-x86iavx512-8-16`, `l-x86iavx512-16-32`, `l-x86iavx512-37-68`, `l-x86iavx512-46-85`, `l-x86iavx512-94-192`
- Balanced pool: `l-x86aavx512-125-463`
- Memory pool: `l-x86iavx512-8-64`, `l-x86iavx512-16-128`, `l-x86iavx512-32-256`, `l-x86iavx512-48-384`, `l-x86iavx512-94-768`

**Improvement**: worst-case utilization 12.6% → 74.4% (+61.8pp)

### Recommendation 2: Split ARM64 Nodepool (MEDIUM IMPACT)

**Current**: 1 nodepool (r7g.16xlarge) for 5 ARM64 runners → worst utilization 37.9%

**Proposed**: Split into 2 nodepools:

| Pool | Instance | Runners | Worst Util | Cost/hr |
|------|----------|---------|-----------|---------|
| Balanced | **m8g.48xlarge** (192c/768Gi) | 3 runners | 88.3% | $8.29 |
| Memory | **r7g.16xlarge** (64c/512Gi) | 1 runner | 75.3% | $3.43 |
| Bare-metal (dedicated) | **m8g.16xlarge** (64c/256Gi) | 1 bare-metal runner | n/a | — |

**Runner assignments**:
- Balanced pool: `l-arm64g2-6-32`, `l-arm64g3-16-62`, `l-arm64g4-16-62`
- Memory pool (keep current): `l-arm64g3-61-463`
- Dedicated bare-metal pool: `l-barm64g4-62-226` runs on `m8g.16xlarge` (one runner per node, for inductor CPU perf benchmarks that need bare-metal noise isolation)

**Improvement**: worst-case utilization 37.9% → 75.3% (+37.4pp). The balanced pool achieves 88-99% utilization for all 4 runners.

### Recommendation 3: Fix T4 Runner Scheduling (DONE)

`l-x86iavx512-48-192-t4-4` was renamed to `l-x86iavx512-45-172-t4-4` with reduced vCPU (45) and memory (172Gi) to leave headroom for the runner pod and system overhead on g4dn.12xlarge. (Per-workflow-pod overhead is now 320m CPU + 522Mi memory from the runner-container-hooks containers; at the time this fix was made it was the 750m / 1Gi runner-orchestrator sidecar — see "How Packing Works" for the current model.)

### Recommendation 4: GPU Nodepool Right-Sizing (DONE)

GPU nodepools are constrained by GPU count, limiting instance options. The original g5.48xlarge and g6.48xlarge assignments were suboptimal for 1-GPU and 4-GPU runners (50-67% utilization), with no smaller GPU instance that fit all runners in a single pool.

**A10G/L4 fleet expansion (implemented)**: The g5 and g6 fleets now include the smaller `g5.8xlarge` / `g5.12xlarge` and `g6.8xlarge` / `g6.12xlarge` sizes as weighted fleet members. 1-GPU and 4-GPU runners are pinned to those smaller instances:
- `l-x86aavx2-29-113-a10g` → `g5.8xlarge` (32c/128Gi, 1 GPU)
- `l-x86aavx2-45-167-a10g-4` → `g5.12xlarge` (48c/192Gi, 4 GPU)
- `l-x86aavx2-189-704-a10g-8` → `g5.48xlarge` (192c/768Gi, 8 GPU)
- `l-x86aavx2-29-113-l4` → `g6.8xlarge`, `l-x86aavx2-45-172-l4-4` → `g6.12xlarge`

**B200 nodepools**: Already well-optimized at 88% utilization. No changes needed — the runners are designed to scale proportionally to GPU count on p6-b200.48xlarge.

## Nodepool Count Impact

| State | CPU Nodepools | GPU Nodepools | Total |
|-------|--------------|---------------|-------|
| Pre-Phase 2 | 2 (r5.24xlarge, r7g.16xlarge) | 6 (g4dn.8xl, g4dn.12xl, g4dn.metal, g5.48xl, g6.48xl, p6-b200) | 8 |
| Phase 2 (proposed at the time) | 9 (3×amx/avx2 + 3×avx512 + 3×arm64) | 6 (unchanged) | 15 |

This increased from 8 to 15 nodepool defs in the original Phase 2 plan (9 CPU pools — including the dedicated bare-metal m8g.16xlarge for `l-barm64g4-62-226` — plus 6 GPU pools). Each nodepool is a Karpenter NodePool CRD — lightweight, no ongoing cost. The infrastructure impact is minimal.

**Post-snapshot state**: After commit `fa950b4` ("Migrate nodepool defs from single-instance to fleet format"), the per-instance-size nodepool defs were collapsed into family-level **fleet** files. Each fleet lists multiple weighted instance sizes (`c7i.48xlarge`, `c7i.24xlarge`, …) and Karpenter picks an appropriate size per workload. The current `modules/nodepools/defs/` directory holds 14 fleet files (`c7a`, `c7i`, `c7i-runner`, `g4dn`, `g5`, `g6`, `m6i`, `m7i`, `m8g`, `p4d-24xlarge`, `r5`, `r7a`, `r7g`, `r7i`), plus separate `nodepools-h100/defs/p5.yaml` and `nodepools-b200/defs/p6.yaml`. The runner-to-instance-size pinning shown above is now a soft preference (fleet weights), not a hard one-pool-per-size split.

**Dedicated runner pool (`c7i-runner`)**: Added after this snapshot for the proactive-capacity / preemption design. It is a name-only clone of the `c7i` fleet, separated by the `node-fleet=c7i-runner` taint, and hosts the lightweight ARC runner-orchestrator pods plus placeholders — workflow pods continue to land on c7a/c7i/m7i/etc. The split is enforced by the `node-fleet=c7i-runner` NodePool taint + matching toleration on runner pods, plus the `CAPACITY_AWARE_RUNNER_NODE_FLEET=c7i-runner` env var on each AutoscalingListener (which pins placeholder-runner and runner-orchestrator pods to that fleet). Because the orchestrator no longer lives on the workflow node, the per-workflow-pod overhead dropped from `750m/1Gi sidecar + hooks` to just the hooks containers (320m/522Mi). See `docs/arc-fork-build-deploy.md` for the full env-var reference.

## Cost Analysis

Direct cost comparison is difficult because runners scale dynamically (Karpenter provisions nodes on demand). However, the key insight is:

**Better utilization = fewer nodes needed for the same number of concurrent runners.**

Example for the AVX-512 compute-heavy runners (`l-x86iavx512-46-85`):
- **Current** (r5.24xlarge): 1 pod per node, 12.6% utilization → pay $6.05/hr for 46c used of 96c
- **Proposed** (c7a.48xlarge): 3 pods per node, 76% utilization → pay $8.24/hr for 138c used of 192c
- **Per-runner cost**: current $6.05/runner-hr → proposed $2.75/runner-hr (**55% savings**)

The savings compound: nodes with better packing serve more concurrent runners, reducing the number of nodes Karpenter needs to provision during peak load. The exact dollar impact depends on runner concurrency patterns.

## Why 85% Utilization Is Hard for Large Runners

Some runners (46c/85Gi, 94c/192Gi, 48c/384Gi) consistently achieve only 74-76% utilization even on perfectly-matched instance types. This is because:

1. **Overhead is fixed**: Kubelet + DaemonSet overhead (~700m CPU, ~1.6Gi memory non-GPU / ~1.9Gi GPU; includes NLD at 25m CPU / 100Mi memory per node) is constant regardless of instance size
2. **Large runners don't divide evenly**: A 94c runner on a 128c node leaves 34c unused — only 1 pod fits
3. **Hooks tax**: Each workflow pod adds 320m CPU + 522Mi memory for runner-container-hooks, which compounds at large sizes (the runner-orchestrator pod itself lives on `c7i-runner`, so workflow nodes are no longer charged the 750m/1Gi orchestrator)

The only way to push these above 85% is to use instances where `N × runner_size ≈ allocatable` — and no standard instance hits that for every runner. The proposed 74% worst-case is the practical optimum.

## Implementation Plan

### Phase 1: Fix T4 Scheduling (DONE)
1. Renamed `l-x86iavx512-48-192-t4-4` → `l-x86iavx512-45-172-t4-4` (45 vCPU, 172Gi)
2. Regenerate and redeploy

### Phase 2: Split x86 CPU + ARM64 Nodepools by Ratio (DONE)
1. Created 8 new nodepool defs: c7i-12xlarge, c7i-metal-24xl, m7i-48xlarge, r7i-48xlarge, c7a-48xlarge, m6i-32xlarge, r7a-48xlarge, m8g-48xlarge (these per-instance defs were later collapsed into family-level fleets; see "Nodepool Count Impact" → Post-snapshot state)
2. Updated 22 x86 runner defs to reference ratio-matched instance types (see Recommendations 1 & 2 above). The bare-metal `l-bx86iamx-92-167` is included in the AMX/AVX2 compute pool, bringing the total x86 CPU runners covered to 23.
3. Updated 4 ARM64 runner defs to m8g.48xlarge; 1 ARM64 runner stays on r7g.16xlarge; the bare-metal `l-barm64g4-62-226` runs on a dedicated m8g.16xlarge (see Recommendation 2).
4. Kept r5-24xlarge nodepool def for RE (Release Engineering) job-assigner workloads (cpu-44, cpu-85); no ARC runners use it
5. Updated r7g-16xlarge node_disk_size from 2660 to 700 (now serves only 1 runner; current value is 1200 after later tuning)

**Not covered by this analysis** (added later): the dedicated **release runner class** (`rel-l-x86iavx512-8-64`, `rel-l-arm64g4-16-62`) introduced in commit `903d1d4`, and the A100 / H100 GPU runners (see GPU Nodepools table for instance details).

**ARM64 silicon refinement** (post-snapshot, PR #591): the Phase 2 decision to run all small ARM64 runners on `m8g.48xlarge` silently substituted Graviton4 for the Graviton2/3 silicon the EC2 labels promised, and the bare-metal `l-barm64g4-62-226` runs on a virtualized `m8g.16xlarge` dedicated node rather than true AWS bare-metal (see pytorch/pytorch#184284). Backings were corrected to match the EC2-promised silicon:
- `l-arm64g2-6-32` → `t4g.2xlarge` (Graviton2, 1 pod/node)
- `l-arm64g3-16-62` → `m7g.8xlarge` (Graviton3, 2 pods/node — `.8xlarge` needed because the 62Gi pod doesn't fit on `m7g.4xlarge` after kubelet overhead)
- `l-barm64g3-62-226` (new pool) → `m7g.metal` (true Graviton3 bare-metal); replaces `l-barm64g4-62-226` as the backing for `linux.arm64.m7g.metal`

This trades the heavy `m8g.48xlarge` bin-packing for honest silicon — per-pod cost on `l-arm64g3-16-62` rises ~30-50% at peak burst, and the standing bare-metal pool carries a fixed cost. See PR #591 for the full rationale.

### Phase 3: Retire Old Nodepools (DONE — merged into Phase 2)
No ARC runners use r5.24xlarge anymore. The nodepool is retained solely for RE job-assigner workloads. r7g.16xlarge kept for the single memory-heavy ARM64 runner. Karpenter will drain underutilized r5 nodes naturally after redeployment.

## Analysis Tools

The original Phase 2 analysis used `scripts/python/analyze_node_utilization.py` (per-node-type packing efficiency) plus a throwaway script (`/tmp/optimize_multi_nodepool.py`, not committed) that evaluated splitting constraint groups into ratio-matched nodepools. The captured results live in this document.

The throwaway script has since been superseded by the simulation tooling now in the repo:

- **`scripts/python/analyze_node_utilization.py`** (`just analyze-utilization`): per-node-type packing efficiency, CPU/memory waste, best/worst mixed combos
- **`scripts/python/simulate_cluster_cli.py`** (`just simulate-cluster`): CLI wrapper that drives full cluster simulation — maps peak concurrency targets to runners, bin-packs onto nodes, reports fleet size and per-runner deployment accuracy; supports Monte Carlo stability runs (`just simulate-cluster --rounds N`)
- **`scripts/python/simulate_cluster.py`**: library backing the CLI (imported by `simulate_cluster_cli.py`)

All scripts model EKS kubelet reserves, DaemonSet overhead, runner sidecars, and ISA/GPU compatibility constraints. See `docs/current_runner_load_distribution.md` for current reproduction commands.

## Runner Name vs Actual Resource Values

Some runner names don't exactly match their actual resource requests (the actual def values are what matter for scheduling):

| Runner Name | Name Implies | Actual Request |
|-------------|-------------|----------------|
| `l-x86iavx512-94-768` | 94c/768Gi | 94c/740Gi |
| `l-bx86iavx512-94-344-t4-8` | 94c/344Gi | 94c/344Gi |
| `l-x86iavx512-94-192` | 94c/192Gi | 94c/189Gi |
| `l-arm64g2-6-32` | 6c/32Gi | 6c/29Gi |
| `l-barm64g4-62-226` | 62c/226Gi | 62c/223Gi |

The analysis scripts use the actual def values. The ratio categorizations are unaffected by these differences.
