# GPU support for the agent sandbox (gVisor nvproxy)

Research notes and design for giving the agent sandbox a GPU, so it can work on tasks that
need one. Measured facts are marked as such; the rest is design or an open question.

Status: the AMI build, fleet, RuntimeClass and dispatcher support described under
"Proposed shape" are written but **nothing has been built or deployed** — no GPU AMI
exists yet, so no cluster can launch one of these nodes. The feature-parity matrix at the
bottom is unrun for the same reason.

## The mechanism, and why everything below follows from it

gVisor's GPU support is **nvproxy**. It does not virtualise the GPU. The sandbox gets
`/dev/nvidiactl`, `/dev/nvidia*` and `/dev/nvidia-uvm`, and the Sentry forwards their
ioctls to the host NVIDIA driver, filtered against an allowlist of ioctl numbers and
struct layouts **specific to a driver version**.

Two consequences run through the rest of this document:

1. The supported driver versions are an explicit list, so driver and runsc have to be
   pinned as a pair.
2. The host NVIDIA kernel driver becomes reachable from sandboxed code. Everything else
   — filesystem, network, all other syscalls — is still handled by the Sentry in
   userspace, so the isolation delta is precisely "the GPU driver".

## Where we stand today (measured 2026-08-21)

| Thing | Value | How it was measured |
|---|---|---|
| Sandbox AMI runsc | `release-20260803.0` | `runsc --version` on an `ai-sandbox` node |
| GPU fleet driver | `580.178.04` | `nvidia-smi` in the device-plugin pod, prod ue1 L4 node |
| GPU fleet AMI | EKS AL2023 **nvidia** variant | `modules/nodepools/defs/*.yaml` name glob |
| Sandbox fleet | `c7a.2xlarge`, `gpu: false`, standard AL2023 base | `modules/nodepools-agent-sandbox/` |

`runsc nvproxy list-supported-drivers` on `release-20260803.0`:

```
535.129.03  535.183.06  535.247.01  535.261.03  535.274.02  535.288.01  535.309.01
550.90.12   570.124.06  570.133.20  570.172.08  570.195.03
580.65.06   580.105.08  580.126.09  580.126.20  580.159.03  580.159.04  580.173.02
590.48.01   615.15.00   620.06.00
```

**Our driver is not on the list** — 580.178.04 against a nearest supported 580.173.02.
The match must be exact: nvproxy parses driver-specific ioctl structs, so it refuses an
unknown version rather than guessing at the layout.

**Bumping runsc does not fix it.** The latest release at the time of writing,
`release-20260817.0`, adds 610.57.04 and 615.62.00 but stops at 580.173.02 on the 580
branch — so 580.178.04 is skipped, not merely not-yet-added. nvproxy supports specific
patch versions and does not backfill every one, which means **the only route today is to
pin the driver down** to a listed version by choosing an older EKS nvidia AMI as the
packer base. Check the list before any future bump of either side; a newer driver is not
automatically a supported one.

## Pinning policy (the standing cost of this feature)

Whichever way we close today's gap — bump runsc to a release that lists our driver, or
pin the AMI down to a release whose driver is already listed — the GPU sandbox AMI is a
**paired pin**: base AMI (driver) and runsc move together, and neither moves alone.

This is a real maintenance obligation, not a one-time fix:

- Every CVE-driven driver bump must land on a version nvproxy supports. The CPU sandbox
  AMI has no such constraint, so the two AMIs will drift apart in cadence.
- "Latest CUDA" and "nvproxy support" pull in opposite directions: CUDA capability
  tracks the driver, and our driver is already ahead of the supported list. Expect to
  run one or two driver releases behind, permanently.
- SSM keeps every historical EKS nvidia AMI
  (`/aws/service/eks/optimized-ami/<k8s>/amazon-linux-2023/x86_64/nvidia/...`), so
  pinning down is easy to express. SSM does not publish the driver version, but the
  `awslabs/amazon-eks-ami` release notes do, which makes the check a `gh api` call
  rather than an instance boot.

### Which AMI to pin

| EKS nvidia AMI | Driver | On nvproxy's list |
|---|---|---|
| `v20260818` (current recommended) | 580.178.04 | no |
| **`v20260810`** | **580.159.03** | **yes** |
| `v20260801` / `v20260728` / `v20260714` | 580.159.03 | yes |

Pin **`v20260810`** — one AMI release behind current, driver 580.159.03. Note AWS never
shipped 580.173.02, the newest 580 nvproxy supports, so 580.159.03 is the best available.

Cost in capability: none worth counting. 580.159.03 is the same **R580** branch as the
driver we run today, and R580 is the CUDA **13.0** branch (13.0 GA shipped with
580.65.06). CUDA 12.x and 11.x toolkits work by backward compatibility, and this repo's
pypi-cache targets (12.6.3, 12.8.1, 13.0.2 in `clusters.yaml`) are all covered, as are
the PyTorch `cu126`/`cu128`/`cu130` wheels. The ceiling is CUDA 13.0 until nvproxy picks
up a driver from a newer branch — its list already carries 590/610/615/620 versions, so
the constraint is which patch versions AWS ships and nvproxy lists, not the branch.

To re-check when either side moves:

```bash
gh api /repos/awslabs/amazon-eks-ami/releases/tags/<tag> --jq .body | grep -i nvidia
runsc nvproxy list-supported-drivers      # runsc is on the sandbox AMI
```

## Threat model: what changes when the sandbox can reach the GPU

The sandbox exists because the agent is untrusted. Adding nvproxy keeps most of that
and gives up one specific thing.

**Unchanged.** No AWS credential in the pod, no Kubernetes token, no RBAC, no IMDS reach
from the pod (IMDSv2 + hop limit 1), Bedrock still signed by the sigv4 proxy, filesystem
and network still Sentry-mediated. "Use secrets without holding them" survives intact.

**Given up.** The NVIDIA kernel driver moves from unreachable to reachable. Concretely:

- **Node takeover through a driver bug.** The driver is a large closed-source kernel
  module with a history of local privilege-escalation CVEs reachable by exactly the
  ioctl access nvproxy grants. Success is ring-0 on the node: every pod on it, the
  kubelet's credentials and therefore those pods' secrets, and the node IAM role via
  IMDS — the hop-limit-1 defence stops a *pod* reaching IMDS, not a compromised node
  kernel. This is the threat that makes a dedicated fleet non-negotiable.
- **Reading a previous task's GPU memory.** VRAM is not reliably zeroed between
  processes on every path (the LeftoverLocals class of issue), so residue can be
  recoverable by a later task on the same GPU.
- **Wedging the device.** Xid errors and unrecoverable ECC states can require replacing
  the node; VRAM exhaustion and crypto mining are trivially available. gVisor never
  defended against these — only fleet isolation, node lifetime and cost alarms do.
- **Side channels** between tasks sharing a GPU (timing, contention).

nvproxy's allowlist is a genuine reduction — unknown ioctls are rejected and parameters
are parsed rather than blindly forwarded — but the allowed set *is* the CUDA-critical
ioctl surface, which is where the historical bugs live. Treat it as attack-surface
reduction, not as a boundary.

### Controls that follow

- **A dedicated GPU sandbox fleet, never shared with CI jobs.** A driver-bug escape
  reaches only other sandbox tasks, not build or test workloads.
- **One task per node.** This removes the VRAM-residue and side-channel items outright,
  and it is why the CPU fleet's 3-slots-per-node packing does not carry over.
- **Short node lifetime.** Consolidate when empty (already the fleet default), and
  consider recycling a node after each task rather than reusing it.
- **Staging only** until the pinning policy above is settled and the test matrix passes.
- **Audit the node IAM role** for these nodes, since node compromise is now the
  realistic worst case rather than a theoretical one.

## Proposed shape

Mirrors the CPU sandbox, with the differences that matter:

- **`modules/nodepools-agent-sandbox/defs/ai-sandbox-gpu.yaml`** — a second fleet,
  `gpu: true`, single-GPU instance type, tainted `node-fleet=ai-sandbox-gpu`. The
  nodepool generator already supports a GPU fleet with a custom AMI: `ami_selector_tags`
  overrides the nvidia name-glob branch, and there is a unit test pinning that.
- **A second RuntimeClass, `gvisor-gpu`.** Not optional:
  `RuntimeClass.scheduling.nodeSelector` on the existing `gvisor` class pins
  `node-fleet: ai-sandbox`, so GPU pods need their own class pinning the GPU fleet.
- **A second packer build** — same script, but based on the EKS AL2023 **nvidia** AMI
  instead of `standard`, with nvproxy enabled in the nodeadm runtime handler options. The
  build fails if the base AMI's driver is not on nvproxy's list. It reads that version
  from the driver RPMs (`nvidia-kmod-common` and friends), because the kernel module is
  DKMS-built at boot: during a build there is no `nvidia.ko` to `modinfo` and no loaded
  driver for `nvidia-smi` to reach. That is also why the builder needs no GPU.
- **A GPU Job template in the dispatcher**, selected by a flag on `/run`
  (`"gpu": true`), requesting `nvidia.com/gpu: 1` and the `gvisor-gpu` RuntimeClass.
  The Job-per-request model already gives per-task pods, so this is a template choice,
  not an architectural change.
- **Quota** sized separately: GPU slots are expensive and the ceiling should be low.

### Why two AMIs rather than one

A single image would have to be the NVIDIA variant with a driver on nvproxy's list, which
drags the **CPU** sandbox onto the same constraint. Today that fleet can rebuild onto the
newest AL2023 base whenever we choose; merging would peg it to whichever AMI release
happens to have a supported driver — currently not the newest one. The CPU AMI's freshness
is that fleet's kernel-CVE story, so coupling it to the GPU driver support matrix trades
away something real for tidiness.

Also: a bad GPU build cannot touch the CPU fleet when the images and tags are separate,
and CPU nodes do not pay for the driver and container toolkit they will never load.

The cost is two near-identical packer templates. If that duplication becomes annoying, the
fix is one template with a `gpu` boolean switching the source AMI, `ENABLE_NVPROXY` and the
tags — one file, still two images, isolation intact. Not worth the conditionals yet.

Note for whoever edits these: name the template in **both** `packer init` and
`packer build`. Bare `.` parses every file in the directory and fails on "Duplicate
variable", because the two templates declare the same inputs.

## Feature parity: what to expect, and what to test

| Capability | Expectation | Confidence |
|---|---|---|
| Single-GPU CUDA compute | Works — this is what nvproxy is for | High |
| Latest CUDA | Lags, structurally (see pinning policy) | High |
| Multi-GPU NCCL on one node | Unverified; needs `cudaIpc*`, nvidia-uvm peer mappings | Unknown |
| NVLink peer access | Unverified, same bucket as NCCL | Unknown |
| Multi-node (EFA / GPUDirect RDMA) | Out — EFA is a separate driver and userspace stack | High |
| MIG | Not needed here | n/a |

The multi-GPU row is the one that decides whether this is useful for advanced tasks, and
it cannot be settled by reading: it needs the AMI to exist and a node to run on.

Acceptance matrix, to run on a GPU sandbox node once one exists:

1. `nvidia-smi` inside a sandboxed pod reports the expected device.
2. A single-GPU torch matmul produces correct results, with timings against the same
   workload under `runc` on the same instance type for overhead.
3. `nvidia-smi topo -m` reports the expected topology.
4. A 2-GPU NCCL all-reduce completes (requires a multi-GPU instance).
5. A `cudaIpc` handle round-trip between two processes in the same pod.

## Open questions

1. ~~Does a newer gVisor release list `580.178.04`?~~ Answered: no, including
   `release-20260817.0`. Pinning the driver down is the only route.
2. ~~Which EKS nvidia AMI release ships a supported driver?~~ Answered: **`v20260810`,
   driver 580.159.03**, which is on nvproxy's list. See "Which AMI to pin" below.
3. Does the NVIDIA device plugin hand devices to a `runsc` pod correctly on EKS
   (containerd runtime handler plus the nvidia container hooks)?
4. What is the compute overhead? nvproxy adds cost on the ioctl path, not to kernels
   already running on the device, so the answer depends on launch patterns.
5. Instance choice for the first cut: one GPU per node, e.g. `g6.4xlarge` (1x L4) —
   which also fixes the slot sizing at one task per node.
