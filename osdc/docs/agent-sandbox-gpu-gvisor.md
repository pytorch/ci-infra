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

## Where we stand today (measured 2026-09-10)

| Thing | Value | How it was measured |
|---|---|---|
| Sandbox AMI runsc | `release-20260831.0` | `gvisor_release` in `packer/*.pkr.hcl` |
| GPU AMI base | `v20260903` (EKS AL2023 **nvidia**) | `nvidia_ami_release` in the GPU template |
| GPU AMI driver | `580.178.04` | `awslabs/amazon-eks-ami` release notes, `kmod-nvidia-latest-dkms` |
| GPU AMI kernel | `6.12.103-127.188.amzn2023` | same release notes, `kernel6.12` |
| Sandbox fleet | `c7a.2xlarge`, `gpu: false`, standard AL2023 base | `modules/nodepools-agent-sandbox/` |

**This was blocked until 2026-08-31 and is not any more.** The original finding was that
our GPU driver, 580.178.04, was on no gVisor release's supported list — nvproxy parses
driver-specific ioctl structs, so it refuses an unknown version rather than guessing at
the layout, and the nearest supported version was 580.173.02. `release-20260831.0` closed
it directly (`nvproxy: add support for driver 580.178.04`), which is why that release is
the floor for `gvisor_release`. 20260824 and earlier still stop at 580.173.02.

Verify either side without booting an instance:

```bash
# driver + kernel shipped by an EKS AMI release
gh api /repos/awslabs/amazon-eks-ami/releases/tags/v20260903 --jq .body \
  | grep -A1 -E 'kmod-nvidia-latest-dkms|kernel6\.12'

# drivers a gVisor release supports (addDriverABI = supported, addUnsupportedDriverABI = not)
gh api "/repos/google/gvisor/contents/pkg/sentry/devices/nvproxy/version.go?ref=release-20260831.0" \
  --jq .content | base64 -d | grep -oE '\baddDriverABI\([0-9]+, [0-9]+, [0-9]+'
```

Versions print as `%02d.%02d.%02d`, so `addDriverABI(580, 178, 04)` is the string
`580.178.04` — the same form the build-time gate compares with `grep -qx`.

### The release tarball, and why the install script unpacks one

`release-20260831.0` also changed what the release bucket publishes. Up to
`release-20260817.0` each release carried bare `runsc` and `containerd-shim-runsc-v1`
objects; from 20260831 it publishes only `gvisor.tar.bz2` (plus a `zstd` variant), so
fetching the binaries by name 404s.

The tarball additionally carries `gvisor-bin/`, which that release made load-bearing: the
sentry is now a separate sidecar binary, resolved in a `gvisor-bin` directory *next to the
runsc binary*. runsc still embeds fallback copies, but `--sidecar-usage-policy` documents
them as slow, defaulting off after 2026-09 and removed after 2026-10. `install-gvisor.sh`
therefore installs `runsc`, the shim and `gvisor-bin/` together into `/usr/local/bin`, and
fails the build if any of the three is missing from the tarball.

## Pinning policy (the standing cost of this feature)

The GPU sandbox AMI is a **paired pin**: base AMI (driver) and runsc move together, and
neither moves alone.

Since CVE-2026-64561 there is a third axis, and it is the one that bites — the base AMI
also carries the **kernel**, so a security bump is not free the way it is on a fleet
tracking `al2023@latest`. All three have to line up at once: a kernel new enough for the
CVE, a driver nvproxy supports, and a runsc release that supports it.

This is a real maintenance obligation, not a one-time fix:

- Every CVE-driven driver bump must land on a version nvproxy supports. The CPU sandbox
  AMI has no such constraint, so the two AMIs will drift apart in cadence.
- "Latest CUDA" and "nvproxy support" pull in opposite directions: CUDA capability tracks
  the driver, and nvproxy lists specific patch versions rather than backfilling every one.
  We are level with the fleet driver today only because 20260831 happened to land before
  we needed it; expect to sit one or two driver releases behind as the normal state.
- A gVisor bump can therefore be on the critical path of a **kernel** CVE, which is the
  awkward part: the fix is unavailable until nvproxy supports the driver that ships
  alongside it. Budget for that when a driver-side CVE lands rather than discovering it
  under an SLO.
- SSM keeps every historical EKS nvidia AMI
  (`/aws/service/eks/optimized-ami/<k8s>/amazon-linux-2023/x86_64/nvidia/...`), so
  pinning down is easy to express. SSM does not publish the driver version, but the
  `awslabs/amazon-eks-ami` release notes do, which makes the check a `gh api` call
  rather than an instance boot.

### Which AMI to pin

| EKS nvidia AMI | Kernel | Driver | CVE-2026-64561 | nvproxy support |
|---|---|---|---|---|
| **`v20260903`** | **6.12.103-127.188** | **580.178.04** | **fixed** | **≥ 20260831** |
| `v20260827` | 6.12.100-125.179 | 580.178.04 | fixed | ≥ 20260831 |
| `v20260818` | 6.12.100-125.179 | 580.178.04 | fixed | ≥ 20260831 |
| `v20260810` | 6.12.95-124.187 | 580.159.03 | **vulnerable** | ≥ 20260803 |
| `v20260801` | 6.12.94-123.192 | 580.159.03 | **vulnerable** | ≥ 20260803 |

Pin **`v20260903`** with runsc **20260831**. Note there is no way to have the kernel fix
and an older driver: every release carrying the fix ships 580.178.04, which is what makes
20260831 a hard floor rather than a preference. Pinning back down to `v20260810` means
knowingly running a kernel with a KVM guest-escape hole on nodes whose whole purpose is to
run untrusted code — do not do it to dodge a gVisor bump.

Cost in capability: none. 580.178.04 is exactly the driver the production GPU fleets run,
so the sandbox is not behind them at all today, and R580 is the CUDA **13.0** branch (13.0
GA shipped with 580.65.06). CUDA 12.x and 11.x toolkits work by backward compatibility,
and this repo's
pypi-cache targets (12.6.3, 12.8.1, 13.0.2 in `clusters.yaml`) are all covered, as are
the PyTorch `cu126`/`cu128`/`cu130` wheels. The ceiling is CUDA 13.0 until nvproxy picks
up a driver from a newer branch — its list already carries 590/610/615/620 versions, so
the constraint is which patch versions AWS ships and nvproxy lists, not the branch.

To re-check when any of the three moves, see the commands under "Where we stand today"
above — or, on a live sandbox node, `runsc nvproxy list-supported-drivers` directly.

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
- **Only callers the policy names may ask for one.** `authorize.py` carries a per-caller
  `gpu` flag and puts the answer on the `Grant`, so the capability is decided beside the
  clone repository and the model rather than taken from the request body — the same
  layering that keeps a caller from naming its own repo. Read default-deny, refused with a
  403 rather than downgraded to CPU, and unavailable to unauthenticated callers even while
  `REQUIRE_AUTH` is false.
- **The API server enforces the two controls above, not just the dispatcher.**
  `kubernetes/base/admissionpolicy.yaml` is the second copy of the task-pod contract, and
  it is where "one device, on the GPU class only" is a rule rather than a convention: a
  device request is admitted only when the key is `nvidia.com/gpu`, the RuntimeClass is
  `gvisor-gpu` — which is what carries the dedicated fleet's nodeSelector — and the count
  is exactly one. A dispatcher bug that asked for four GPUs, or for one on the CPU class,
  is rejected at admission rather than scheduled.

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
  `requests.nvidia.com/gpu` in the namespace quota is the ceiling that actually holds,
  independently of what the dispatcher believes its own limit to be.
- **Two amended rules in the admission policy** — the RuntimeClass allowlist gains
  `gvisor-gpu`, and the resource-key allowlist gains `nvidia.com/gpu` under the three
  conditions above. Both are mirrored in `dispatcher/test_admissionpolicy.py` and probed
  live by the smoke suite, which is the only place the CEL is actually evaluated.

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
