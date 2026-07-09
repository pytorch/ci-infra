Runner labels encode hardware capabilities directly in the name, so a workflow author knows exactly what they're getting without looking up instance types.

## Why the Names Are So Compact

Runner labels stay under **~42 characters** to leave headroom under several stacking constraints in how ARC (Actions Runner Controller) derives names from the scale set name:

1. **ARC Helm chart** enforces a **45-character** maximum on scale set names. With ARC, the scale set name *is* the `runs-on` label — you cannot add extra labels to target ARC runners. See `charts/gha-runner-scale-set/templates/autoscalingrunnerset.yaml` lines 6-8 in the [actions-runner-controller fork](https://github.com/jeanschmidt/actions-runner-controller).
2. **Kubernetes label values** are capped at **63 characters**. The ARC chart derives resource names as `{scale-set-name}-gha-rs` (7-char infix) plus a per-resource suffix; the longest suffix is `-no-permission` (14 chars), giving a derived ServiceAccount name of `{scale-set-name}-gha-rs-no-permission` — 21 chars added in total. A 45-char scale-set name therefore produces a 66-char derived name, which the chart truncates to 63 via `trunc 63 | trimSuffix "-"` (see `_helpers.tpl` lines 22-25 and 83-85). Truncation works for the chart's internal references but breaks any downstream consumer that mirrors the un-truncated derived name as a label value.
3. **Future CNI-based network policy** (planned migration to Cilium for `toFQDNs` enforcement in cache-enforcer) would create `CiliumIdentity` resources using the ServiceAccount name as a label value, re-exposing the 63-char cap. OSDC does not run Cilium today, so this is not a today-blocking limit; the ~42-character target is headroom that keeps the option open without renaming runners later.

Every field is abbreviated (`l` not `linux`, `avx512` not `avx-512`, no units on vCPU/memory). The longest label in any deployed cluster is 33 characters: `c-mt-l-bx86iavx512-88-1000-a100-8` on staging (which uses the `c-mt-` canary prefix). Production (`mt-` prefix) tops out at 31 characters (`mt-l-bx86iavx512-88-1000-a100-8`). Both are well within the ceiling.

Reference: [actions/actions-runner-controller#2697](https://github.com/actions/actions-runner-controller/issues/2697)

## Format

`[c-]{provider}-[rel-]{os}-[b]{arch}{vendor}{features}-{vcpu}-{memory}[-{gpu_type}[-{gpu_count}]]`

Defs live in `modules/arc-runners/defs/` (CPU runners + A100 GPU + release defs), `modules/arc-runners-h100/defs/` (H100 GPU), and `modules/arc-runners-b200/defs/` (B200 GPU). The naming convention is identical across all three modules — the split is operational (per-module capacity reservation handling), not semantic.

## Fields


| **Field** | **Required** | **Description** | **Values** |
| --- | --- | --- | --- |
| `c` (prefix) | No | Canary runner (staging / testing). Omitted for production runners. | `c` = canary, omitted = production |
| `provider` | Yes | Organization that operates and funds the runner fleet. Currently only `mt` is in active use across OSDC clusters (see `clusters.yaml`); the other codes are reserved for future provider-funded fleets. | `mt` = Meta (in use), reserved: `lf` = Linux Foundation, `am` = AMD, `in` = Intel, `nv` = NVIDIA, `ib` = IBM |
| `rel` (prefix) | No | Release runner — dedicated runner group (`release-runners`) and node isolation (`osdc.io/runner-class: release`). Omitted for CI runners. | `rel` = release, omitted = CI |
| `os` | Yes | Operating system | `l` = Linux, `w` = Windows, `m` = MacOS  |
| `b` (prefix) | No | Bare-metal / dedicated instance (gets the full node — no bin-packing) | `b` = bare-metal, omitted = shared (multiple runners per node) |
| `arch` | Yes | CPU architecture | `x86` = x86_64, `arm64` = AArch64 |
| `vendor` | Yes | ISA family / generation, **not silicon vendor** (for ARM, this is the Graviton generation). The letter encodes the ISA convention used historically by the runner class — the underlying NodePool may run on a different vendor's silicon (e.g. `i`-named AVX-512 runners commonly run on AMD c7a/r7a; `a`-named runners may run on Intel m6i). | `i` = Intel-style ISA (AVX-512 / AMX runner classes; may run on AMD silicon supporting the same ISA), `a` = AMD-style ISA (m6a/m7a-style classes; may also run on Intel), `g2` = Graviton 2, `g3` = Graviton 3, `g4` = Graviton 4 |
| `features` | Yes (x86 only) | CPU instruction set extensions — tells the workflow what SIMD/AI instructions are available | `avx2` = AVX2, `avx512` = AVX-512, `amx` = Intel AMX (Advanced Matrix Extensions) |
| `vcpu` | Yes | Number of vCPUs allocated to the runner | Integer (e.g. `2`, `8`, `16`, `48`, `94`) |
| `memory` | Yes | Memory in GiB allocated to the runner | Integer (e.g. `4`, `16`, `64`, `192`, `768`) |
| `gpu_type` | No | GPU model (omitted for CPU-only runners) | `t4` = NVIDIA T4, `a10g` = NVIDIA A10G, `l4` = NVIDIA L4, `a100` = NVIDIA A100 40GB, `h100` = NVIDIA H100, `b200` = NVIDIA B200 |
| `gpu_count` | No | Number of GPUs (omitted when count is 1) | Integer (e.g. `4`, `8`) |

## Examples


| **Label** | **Breakdown** |
| --- | --- |
| `mt-l-x86iavx512-8-16` | Meta, Linux, x86 Intel AVX-512, 8 vCPU, 16 GiB |
| `mt-l-arm64g3-16-62` | Meta, Linux, ARM64 Graviton 3, 16 vCPU, 62 GiB |
| `nv-l-x86aavx2-48-192-a10g-4` | NVIDIA, Linux, x86 AMD AVX2, 48 vCPU, 192 GiB, 4x A10G GPUs |
| `c-mt-l-x86iavx512-8-16` | **Canary**, Meta, Linux, x86 Intel AVX-512, 8 vCPU, 16 GiB |
| `mt-rel-l-arm64g3-44-340` | Meta, **Release**, Linux, ARM64 Graviton 3, 44 vCPU, 340 GiB |
| `mt-rel-l-x86iavx512-44-340` | Meta, **Release**, Linux, x86 Intel AVX-512, 44 vCPU, 340 GiB |
| `l-x86iamx-14-27` | Linux, x86 Intel AMX, 14 vCPU, 27 GiB |

## Old Label → New Label Mapping

Maps each `scale-config.yml` runner label to its OSDC ARC equivalent, matched by vCPU, memory, and GPU resources. Linux only (Windows runners not yet on ARC).

### x86 CPU — Intel AVX-512 (c5, c7i families)


| **Old Label** | **Instance** | **New Label** |
| --- | --- | --- |
| linux.large | c5.large | mt-l-x86iavx512-2-4 |
| linux.c7i.large | c7i.large | mt-l-x86iavx512-2-4 |
| linux.2xlarge | c5.2xlarge | mt-l-x86iavx512-8-16 |
| linux.c7i.2xlarge | c7i.2xlarge | mt-l-x86iavx512-8-16 |
| linux.4xlarge | c5.4xlarge | mt-l-x86iavx512-16-32 |
| linux.4xlarge.for.testing.donotuse | c5.4xlarge | mt-l-x86iavx512-16-32 |
| linux.c7i.4xlarge | c7i.4xlarge | mt-l-x86iavx512-16-32 |
| linux.c7i.8xlarge | c7i.8xlarge | *— no equivalent* |
| linux.9xlarge.ephemeral | c5.9xlarge | mt-l-x86iavx512-37-68 |
| linux.12xlarge | c5.12xlarge | mt-l-x86iavx512-46-85 |
| linux.12xlarge.ephemeral | c5.12xlarge | mt-l-x86iavx512-46-85 |
| linux.c7i.12xlarge | c7i.12xlarge | mt-l-x86iavx512-46-85 |
| linux.16xlarge.spr | c7i.16xlarge | *— no equivalent* |
| linux.24xlarge | c5.24xlarge | mt-l-x86iavx512-94-192 |
| linux.24xlarge.ephemeral | c5.24xlarge | mt-l-x86iavx512-94-192 |
| linux.c7i.24xlarge | c7i.24xlarge | mt-l-x86iavx512-94-192 |
| linux.24xl.spr-metal | c7i.metal-24xl | mt-l-bx86iamx-92-167 |

### x86 CPU — Intel AMX (m7i-flex family)


| **Old Label** | **Instance** | **New Label** |
| --- | --- | --- |
| linux.2xlarge.amx | m7i-flex.2xlarge | mt-l-x86iamx-8-32 |
| linux.4xlarge.amx | m7i-flex.4xlarge | *— no equivalent* |
| linux.8xlarge.amx | m7i-flex.8xlarge | mt-l-x86iamx-32-128 |

Additional AMX runners (no direct old-label mapping; provisioned for OSDC CI workloads):

| **New Label** | **Instance** | **vCPU / memory** |
| --- | --- | --- |
| mt-l-x86iamx-8-16 | c7i.12xlarge | 8 / 16 GiB |
| mt-l-x86iamx-14-27 | c7i.12xlarge | 14 / 27 GiB |
| mt-l-x86iamx-22-41 | c7i.12xlarge | 22 / 41 GiB |
| mt-l-x86iamx-46-84 | c7i.12xlarge | 46 / 84 GiB |
| mt-l-x86iamx-8-64 | r7i.48xlarge | 8 / 64 GiB |
| mt-l-x86iamx-16-128 | r7i.48xlarge | 16 / 128 GiB |

### x86 CPU — Intel AVX2 (m4 family)


| **Old Label** | **Instance** | **New Label** |
| --- | --- | --- |
| linux.2xlarge.avx2 | m4.2xlarge | mt-l-x86iavx2-8-32 |
| linux.4xlarge.avx2 | m4.4xlarge | *— no equivalent* |
| linux.10xlarge.avx2 | m4.10xlarge | mt-l-x86iavx2-40-160 |

### x86 CPU — Memory-optimized (r5, r7i families)


| **Old Label** | **Instance** | **New Label** |
| --- | --- | --- |
| linux.r7i.large | r7i.large | *— no equivalent* |
| linux.r7i.xlarge | r7i.xlarge | *— no equivalent* |
| linux.r7i.2xlarge | r7i.2xlarge | mt-l-x86iavx512-8-64 |
| linux.r7i.4xlarge | r7i.4xlarge | mt-l-x86iavx512-16-128 |
| linux.r7i.8xlarge | r7i.8xlarge | mt-l-x86iavx512-32-256 |
| linux.r7i.12xlarge | r7i.12xlarge | mt-l-x86iavx512-48-384 |
| linux.2xlarge.memory | r5.2xlarge | mt-l-x86iavx512-8-64 |
| linux.4xlarge.memory | r5.4xlarge | mt-l-x86iavx512-16-128 |
| linux.8xlarge.memory | r5.8xlarge | mt-l-x86iavx512-32-256 |
| linux.12xlarge.memory | r5.12xlarge | mt-l-x86iavx512-48-384 |
| linux.12xlarge.memory.ephemeral | r5.12xlarge | mt-l-x86iavx512-48-384 |
| linux.16xlarge.memory | r5.16xlarge | *— no equivalent* |
| linux.24xlarge.memory | r5.24xlarge | mt-l-x86iavx512-94-768 |

### x86 CPU — AMD (m6a, m7a families)


| **Old Label** | **Instance** | **New Label** |
| --- | --- | --- |
| linux.8xlarge.amd | m7a.8xlarge | *— no equivalent* |
| linux.12xlarge.amd | m6a.12xlarge | *— no equivalent* |
| linux.24xlarge.amd | m6i.32xlarge | mt-l-x86aavx512-125-463 |

### x86 GPU — T4 (g4dn family)


| **Old Label** | **Instance** | **New Label** |
| --- | --- | --- |
| linux.4xlarge.nvidia.gpu | g4dn.4xlarge | mt-l-x86iavx512-29-115-t4 |
| linux.g4dn.4xlarge.nvidia.gpu | g4dn.4xlarge | mt-l-x86iavx512-29-115-t4 |
| linux.g4dn.12xlarge.nvidia.gpu | g4dn.12xlarge | mt-l-x86iavx512-45-172-t4-4 |
| linux.g4dn.metal.nvidia.gpu | g4dn.metal | mt-l-bx86iavx512-94-344-t4-8 |

### x86 GPU — A10G (g5 family)


| **Old Label** | **Instance** | **New Label** |
| --- | --- | --- |
| linux.g5.4xlarge.nvidia.gpu | g5.4xlarge | mt-l-x86aavx2-29-113-a10g |
| linux.g5.12xlarge.nvidia.gpu | g5.12xlarge | mt-l-x86aavx2-45-167-a10g-4 |
| linux.g5.48xlarge.nvidia.gpu | g5.48xlarge | mt-l-x86aavx2-189-704-a10g-8 |

### x86 GPU — L4 (g6 family)


| **Old Label** | **Instance** | **New Label** |
| --- | --- | --- |
| linux.g6.4xlarge.experimental.nvidia.gpu | g6.4xlarge | mt-l-x86aavx2-29-113-l4 |
| linux.g6.12xlarge.nvidia.gpu | g6.12xlarge | mt-l-x86aavx2-45-172-l4-4 |

### x86 GPU — B200 (p6-b200 family)


| **Old Label** | **Instance** | **New Label** |
| --- | --- | --- |
| a.linux.b200 | p6-b200.48xlarge | mt-l-x86iamx-22-225-b200 |
| a.linux.b200.2 | p6-b200.48xlarge | mt-l-x86iamx-44-450-b200-2 |
| a.linux.b200.4 | p6-b200.48xlarge | mt-l-x86iamx-88-900-b200-4 |
| a.linux.b200.8 | p6-b200.48xlarge | mt-l-bx86iamx-176-1800-b200-8 |

### x86 GPU — H100 (p5 family)


| **Old Label** | **Instance** | **New Label** |
| --- | --- | --- |
| a.linux.h100 | p5.48xlarge | mt-l-x86iamx-22-225-h100 |
| a.linux.h100.2 | p5.48xlarge | mt-l-x86iamx-44-450-h100-2 |
| a.linux.h100.4 | p5.48xlarge | mt-l-x86iamx-88-900-h100-4 |
| a.linux.h100.8 | p5.48xlarge | mt-l-bx86iamx-176-1800-h100-8 |

### x86 GPU — A100 (p4d family)


| **Old Label** | **Instance** | **New Label** |
| --- | --- | --- |
| a.linux.a100 | p4d.24xlarge | mt-l-x86iavx512-11-125-a100 |
| a.linux.a100.2 | p4d.24xlarge | mt-l-x86iavx512-22-250-a100-2 |
| a.linux.a100.4 | p4d.24xlarge | mt-l-x86iavx512-44-500-a100-4 |
| a.linux.a100.8 | p4d.24xlarge | mt-l-bx86iavx512-88-1000-a100-8 |

### x86 GPU — V100 (p3 family)


| **Old Label** | **Instance** | **New Label** |
| --- | --- | --- |
| linux.p3.8xlarge.nvidia.gpu | p3.8xlarge | *— no equivalent* |

### ARM64 (Graviton)


| **Old Label** | **Instance** | **New Label** |
| --- | --- | --- |
| linux.arm64.2xlarge | t4g.2xlarge | mt-l-arm64g2-6-32 |
| linux.arm64.2xlarge.ephemeral | t4g.2xlarge | mt-l-arm64g2-6-32 |
| linux.arm64.m7g.4xlarge | m7g.4xlarge | mt-l-arm64g3-16-62 |
| linux.arm64.m7g.4xlarge.ephemeral | m7g.4xlarge | mt-l-arm64g3-16-62 |
| linux.arm64.m8g.4xlarge | m8g.4xlarge | mt-l-arm64g4-16-62 |
| linux.arm64.m8g.4xlarge.ephemeral | m8g.4xlarge | mt-l-arm64g4-16-62 |
| linux.arm64.r7g.12xlarge.memory | r7g.16xlarge | mt-l-arm64g3-61-463 |
| linux.arm64.m7g.metal | m7g.metal | mt-l-barm64g3-62-226 |

## Many-to-One: Old Labels That Collapse Into a Single New Label

Several old labels map to the same new label. This is by design — the new naming describes **what the runner provides** (CPU features, vCPU, memory), not which AWS instance it runs on. From the workflow's perspective, 8 vCPU + 64 GiB on an r5 is the same as 8 vCPU + 64 GiB on an r7i. The old system leaked the instance type into the label, creating artificial distinctions that workflows shouldn't care about.

Excluding trivial `.ephemeral` / `.nonephemeral` duplicates, these are the cases where different instance families collapse:


| **New Label** | **Old Labels** | **Old Instance Types** |
| --- | --- | --- |
| mt-l-x86iavx512-2-4 | linux.large, linux.c7i.large | c5.large, c7i.large |
| mt-l-x86iavx512-8-16 | linux.2xlarge, linux.c7i.2xlarge | c5.2xlarge, c7i.2xlarge |
| mt-l-x86iavx512-16-32 | linux.4xlarge, linux.4xlarge.for.testing.donotuse, linux.c7i.4xlarge | c5.4xlarge, c7i.4xlarge |
| mt-l-x86iavx512-46-85 | linux.12xlarge, linux.c7i.12xlarge | c5.12xlarge, c7i.12xlarge |
| mt-l-x86iavx512-94-192 | linux.24xlarge, linux.c7i.24xlarge | c5.24xlarge, c7i.24xlarge |
| mt-l-x86iavx512-8-64 | linux.r7i.2xlarge, linux.2xlarge.memory | r7i.2xlarge, r5.2xlarge |
| mt-l-x86iavx512-16-128 | linux.r7i.4xlarge, linux.4xlarge.memory | r7i.4xlarge, r5.4xlarge |
| mt-l-x86iavx512-32-256 | linux.r7i.8xlarge, linux.8xlarge.memory | r7i.8xlarge, r5.8xlarge |
| mt-l-x86iavx512-48-384 | linux.r7i.12xlarge, linux.12xlarge.memory | r7i.12xlarge, r5.12xlarge |
| mt-l-x86iavx512-29-115-t4 | linux.4xlarge.nvidia.gpu, linux.g4dn.4xlarge.nvidia.gpu | g4dn.4xlarge |

**Note on r7i vs r5:** r7i is Sapphire Rapids (has AMX), while r5 is Cascade Lake (AVX-512 only). Both are mapped to `x86iavx512` in our current defs because the underlying OSDC memory-optimized NodePool runs on `r7a.48xlarge` (AMD EPYC, AVX-512 only — no AMX) regardless. The previous `r5.24xlarge` NodePool has been retired for ARC use (it is retained only for RE job-assigner workloads — see `docs/node-utilization-optimization.md`). The label reflects what is actually delivered, not what the old instance type could do.

### What do workflows actually lose?

There are two categories of collapse, with different implications:

**1. The **`.ephemeral`** / **`.nonephemeral`** duplicates — no difference at all.** Both members of each pair run the exact same instance type with `is_ephemeral: true` in scale-config.yml. They were created as separate labels historically (likely during a migration from persistent to ephemeral runners), but the config is identical. Workflows just happened to pick one label or the other. Pure cruft.

**2. The cross-instance-family collapses — the difference is CPU microarchitecture.** In the old system, a workflow author could target a specific CPU generation by picking the right label (e.g., `linux.r7i.2xlarge` for Sapphire Rapids with AMX vs `linux.2xlarge.memory` for Cascade Lake with AVX-512). In OSDC, the NodePool decides the instance type — and right now the memory-optimized NodePool runs on AMD `r7a.48xlarge` (AVX-512, no AMX), so both old labels collapse to `mt-...-x86iavx512-...`. A workflow that previously ran on r7i and depended on AMX instructions would silently lose that capability.

In practice, **no PyTorch CI workflow uses AMX instructions directly** — the r7i and c7i runners were added as cheaper/newer alternatives, not because workflows needed AMX. The collapse is safe. If AMX becomes important later, the naming convention already supports it: create a separate `x86iamx` runner backed by a NodePool targeting Sapphire Rapids or newer instances.

Same story for c5 vs c7i compute runners — different CPU generations, but the new label reflects what the NodePool actually provides, not what the old label promised.
