# Node Utilization Optimization

Analysis of runner-to-node packing efficiency across all OSDC nodepools, with recommendations for instance type changes to reduce waste.

## Problem Statement

OSDC uses Kubernetes Guaranteed QoS pods (requests == limits) for all runner workloads. Each runner type has fixed CPU and memory requirements, and each nodepool uses a single AWS instance type. When runners with different CPU:memory ratios share a nodepool, the mismatched resource is wasted.

**Original state** (before Phase 2): 36 of 39 runner types had homogeneous utilization below 90%. Several had worst-case utilization under 20%. After Phase 2 (ratio-matched nodepools), x86 CPU worst-case improved from 12.6% to ~74% and ARM64 from 37.9% to 75.3%.

> **Note**: This analysis covers both upstream runners (35 in `modules/arc-runners/defs/`) and consumer B200 runners (4 in the consumer repo's `modules/arc-runners-b200/defs/`). B200 nodepool and runner defs are not in the upstream repo.

## How Packing Works

Each node's allocatable resources are reduced by:

1. **Kubelet reserved CPU**: 60m (first core) + 10m/core (cores 2-4) + 5m/core (cores 5-8) + 2.5m/core (cores 9+)
2. **Kubelet reserved memory**: 255Mi + 11Mi per max_pod (ENI-based) + 100Mi eviction threshold
3. **DaemonSet overhead**: 245m CPU / 640Mi memory (non-GPU), 345m CPU / 768Mi memory (GPU nodes)

Each runner pod also includes a sidecar (750m CPU, 512Mi memory) on top of its job container resources.

**Utilization** = `(pods_per_node * pod_resource) / allocatable_resource` for each dimension. The **minimum** across CPU and memory determines the packing efficiency — unused resources in the higher dimension are waste.

## Root Cause: CPU:Memory Ratio Mismatch

Runners fall into three ratio categories:

| Category | GiB/core | Example Runners | Best Instance Family |
|----------|----------|----------------|---------------------|
| Compute-heavy | ~2 GiB/core | 48c/96Gi, 94c/192Gi, 8c/16Gi | c-series (c7i, c7a) |
| Balanced | ~4 GiB/core | 8c/32Gi, 32c/128Gi, 40c/160Gi | m-series (m7i, m7a, m8g) |
| Memory-heavy | ~8 GiB/core | 8c/64Gi, 16c/128Gi, 48c/384Gi | r-series (r7i, r7a, r7g) |

**Original assignment** (before Phase 2): All CPU runners shared one r5.24xlarge (96c/768Gi, ratio 8:1). This was perfect for memory-heavy runners but wasted ~75% of memory for compute-heavy runners like `l-x86iavx512-48-96` (48c/96Gi, ratio 2:1). Phase 2 split these into ratio-matched nodepools (c/m/r series per ISA group).

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
| g4dn.12xlarge | 48c/192Gi @ $3.91/hr | 2 (T4) | **67.8%** | `l-x86iavx512-16-64-t4` fits at 68%; `l-x86iavx512-45-187-t4-4` fits with headroom (FIXED) |
| g4dn.metal | 96c/384Gi @ $7.82/hr | 1 (T4) | 96.0% | Good — `l-bx86iavx512-94-384-t4-8` fills the node |
| p6-b200.48xlarge | 192c/2048Gi @ $55.47/hr | 4 (B200) | 88.1% | Good — runners scale proportionally to GPU count |

### Critical Issue: T4 Runner Cannot Schedule

`l-x86iavx512-48-192-t4-4` requests 48 vCPU + 192Gi memory + 4 GPUs. After adding the 750m sidecar (total: 48750m CPU), it exceeds the g4dn.12xlarge allocatable CPU of 47465m. **This runner can never schedule on g4dn.12xlarge.**

The other T4 runner on g4dn.12xlarge — `l-x86iavx512-16-64-t4` (16c/64Gi, 1 GPU) — schedules fine with 2 pods at 68% utilization.

Options:
1. Move the 4-GPU runner to g4dn.metal (96c, 8 GPU) — wastes half the node but it will schedule
2. Reduce its CPU request to 46 vCPU (46750m with sidecar fits in 47465m allocatable)
3. Accept that it can only schedule on g4dn.metal alongside the 94c/366Gi runner

## Recommendations

### Recommendation 1: Split x86 CPU Nodepools by Ratio (HIGH IMPACT)

**Current**: 1 nodepool (r5.24xlarge) for all 22 x86 CPU runners → worst utilization 12.6%

**Proposed**: Split into 3 nodepools per ISA group, matched to CPU:memory ratio.

#### AMX/AVX2 Group (10 runners → 3 nodepools)

These runners include `l-x86iamx-*` (AMX) and `l-x86iavx2-*` (AVX2). Since AMX runners require Intel, **all pools in this group must use Intel instances** (c7i, m7i, r7i — not AMD c7a, m7a, r7a).

| Pool | Instance | Runners | Worst Util | Cost/hr |
|------|----------|---------|-----------|---------|
| Compute | **c7i.12xlarge** (48c/96Gi) + **c7i.metal-24xl** (96c/192Gi) | 5 shared + 1 bare-metal (2:1 ratio) | ~92-99% | $2.14 / $4.28 |
| Balanced | **m7i.48xlarge** (192c/768Gi) | 4 runners (4:1 ratio) | ~84% | $9.68 |
| Memory | **r7i.48xlarge** (192c/1536Gi) | 2 runners (8:1 ratio) | ~88% | $12.70 |

**Runner assignments**:
- Compute pool: `l-x86iamx-8-17`, `l-x86iamx-14-30`, `l-x86iamx-22-45`, `l-x86iamx-46-91` (c7i.12xlarge), `l-bx86iamx-92-180` (c7i.metal-24xl)
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
- Compute pool: `l-x86iavx512-2-4`, `l-x86iavx512-8-16`, `l-x86iavx512-16-32`, `l-x86iavx512-36-72`, `l-x86iavx512-48-96`, `l-x86iavx512-94-192`
- Balanced pool: `l-x86aavx512-94-384`
- Memory pool: `l-x86iavx512-8-64`, `l-x86iavx512-16-128`, `l-x86iavx512-32-256`, `l-x86iavx512-48-384`, `l-x86iavx512-94-768`

**Improvement**: worst-case utilization 12.6% → 74.4% (+61.8pp)

### Recommendation 2: Split ARM64 Nodepool (MEDIUM IMPACT)

**Current**: 1 nodepool (r7g.16xlarge) for 5 ARM64 runners → worst utilization 37.9%

**Proposed**: Split into 2 nodepools:

| Pool | Instance | Runners | Worst Util | Cost/hr |
|------|----------|---------|-----------|---------|
| Balanced | **m8g.48xlarge** (192c/768Gi) | 4 runners | 88.3% | $8.29 |
| Memory | **r7g.16xlarge** (64c/512Gi) | 1 runner | 75.3% | $3.43 |

**Runner assignments**:
- Balanced pool: `l-arm64g2-6-32`, `l-arm64g3-16-64`, `l-arm64g4-16-64`, `l-barm64g3-62-256`
- Memory pool (keep current): `l-arm64g3-48-384`

**Improvement**: worst-case utilization 37.9% → 75.3% (+37.4pp). The balanced pool achieves 88-99% utilization for all 4 runners.

### Recommendation 3: Fix T4 Runner Scheduling (DONE)

`l-x86iavx512-48-192-t4-4` was renamed to `l-x86iavx512-45-187-t4-4` with reduced vCPU (45) and memory (187Gi) to leave headroom for the runner pod (750m/512Mi) and system overhead on g4dn.12xlarge (allocatable ~47.5c/~186Gi).

### Recommendation 4: GPU Nodepool Right-Sizing (LOW PRIORITY)

GPU nodepools are constrained by GPU count, limiting instance options. The current g5.48xlarge and g6.48xlarge assignments are suboptimal for 1-GPU and 4-GPU runners (50-67% utilization), but there are no smaller GPU instances that fit all runners in a single pool.

**Potential optimization for A10G**: Use g5.8xlarge (32c/128Gi, 1 GPU, $2.45/hr) for the 1-GPU runner and g5.12xlarge (48c/192Gi, 4 GPU, $5.67/hr) for the 4-GPU runner instead of g5.48xlarge ($16.29/hr). This adds 2 nodepools but each node is 3-7x cheaper. Worth evaluating if A10G runners are used frequently.

**B200 nodepools**: Already well-optimized at 88% utilization. No changes needed — the runners are designed to scale proportionally to GPU count on p6-b200.48xlarge.

## Nodepool Count Impact

| State | CPU Nodepools | GPU Nodepools | Total |
|-------|--------------|---------------|-------|
| Current | 2 (r5.24xlarge, r7g.16xlarge) | 5 (g4dn.12xl, g4dn.metal, g5.48xl, g6.48xl, p6-b200) | 7 |
| Proposed | 8 (3×amx/avx2 + 3×avx512 + 2×arm64) | 5 (unchanged) | 13 |

This increases from 7 to 13 nodepools (+6). Each nodepool is a Karpenter NodePool CRD — lightweight, no ongoing cost. The infrastructure impact is minimal.

## Cost Analysis

Direct cost comparison is difficult because runners scale dynamically (Karpenter provisions nodes on demand). However, the key insight is:

**Better utilization = fewer nodes needed for the same number of concurrent runners.**

Example for the AVX-512 compute-heavy runners (`l-x86iavx512-48-96`):
- **Current** (r5.24xlarge): 1 pod per node, 12.6% utilization → pay $6.05/hr for 48c used of 96c
- **Proposed** (c7a.48xlarge): 3 pods per node, 76% utilization → pay $8.24/hr for 144c used of 192c
- **Per-runner cost**: current $6.05/runner-hr → proposed $2.75/runner-hr (**55% savings**)

The savings compound: nodes with better packing serve more concurrent runners, reducing the number of nodes Karpenter needs to provision during peak load. The exact dollar impact depends on runner concurrency patterns.

## Why 85% Utilization Is Hard for Large Runners

Some runners (48c/96Gi, 94c/192Gi, 94c/384Gi) consistently achieve only 74-76% utilization even on perfectly-matched instance types. This is because:

1. **Overhead is fixed**: Kubelet + DaemonSet overhead (~500m CPU, ~1.5Gi memory) is constant regardless of instance size
2. **Large runners don't divide evenly**: A 94c runner on a 128c node leaves 34c unused — only 1 pod fits
3. **Sidecar tax**: Each pod adds 750m CPU + 512Mi memory, which compounds at large sizes

The only way to push these above 85% is to use instances where `N × runner_size ≈ allocatable` — and no standard instance hits that for every runner. The proposed 74% worst-case is the practical optimum.

## Implementation Plan

### Phase 1: Fix T4 Scheduling (DONE)
1. Renamed `l-x86iavx512-48-192-t4-4` → `l-x86iavx512-45-187-t4-4` (45 vCPU, 187Gi)
2. Regenerate and redeploy

### Phase 2: Split x86 CPU + ARM64 Nodepools by Ratio (DONE)
1. Created 8 new nodepool defs: c7i-12xlarge, c7i-metal-24xl, m7i-48xlarge, r7i-48xlarge, c7a-48xlarge, m6i-32xlarge, r7a-48xlarge, m8g-48xlarge
2. Updated 22 x86 runner defs to reference ratio-matched instance types (see Recommendations 1 & 2 above)
3. Updated 4 ARM64 runner defs to m8g.48xlarge; 1 ARM64 runner stays on r7g.16xlarge
4. Kept r5-24xlarge nodepool def for RE (Release Engineering) job-assigner workloads (cpu-44, cpu-85); no ARC runners use it
5. Updated r7g-16xlarge node_disk_size from 2660 to 700 (now serves only 1 runner)

### Phase 3: Retire Old Nodepools (DONE — merged into Phase 2)
No ARC runners use r5.24xlarge anymore. The nodepool is retained solely for RE job-assigner workloads. r7g.16xlarge kept for the single memory-heavy ARM64 runner. Karpenter will drain underutilized r5 nodes naturally after redeployment.

## Analysis Tools

Two Python scripts were used for this analysis:

- **`scripts/python/analyze_node_utilization.py`**: Reads runner and nodepool defs, computes packing efficiency per node type
- **`/tmp/optimize_multi_nodepool.py`**: Evaluates splitting constraint groups into multiple nodepools with ratio-matched instance families (c/m/r series). Not committed — results are captured in this document.

Both scripts model EKS kubelet reserves, DaemonSet overhead, runner sidecars, and ISA/GPU compatibility constraints.

## Runner Name vs Actual Resource Values

Some runner names don't exactly match their actual resource requests (the actual def values are what matter for scheduling):

| Runner Name | Name Implies | Actual Request |
|-------------|-------------|----------------|
| `l-x86iavx512-94-768` | 94c/768Gi | 94c/740Gi |
| `l-bx86iavx512-94-384-t4-8` | 94c/384Gi | 94c/366Gi |
| `l-x86iavx512-94-192` | 94c/192Gi | 94c/189Gi |
| `l-arm64g2-6-32` | 6c/32Gi | 6c/29Gi |
| `l-barm64g3-62-256` | 62c/256Gi | 62c/253Gi |

The analysis scripts use the actual def values. The ratio categorizations are unaffected by these differences.
