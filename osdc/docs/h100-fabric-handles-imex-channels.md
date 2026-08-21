# H100 Fabric Handles and IMEX Channels (OSDC)

## Context

PyTorch [pytorch/pytorch#190860](https://github.com/pytorch/pytorch/pull/190860) fixed a
destructor crash in `ExpandableSegment::unmapHandles` — `std::bad_variant_access` when the
cached `shareable_handle` holds a `CUmemFabricHandle` instead of a POSIX fd. A follow-up
question asked whether OSDC could run CI that exercises the CUDA **fabric handle** path, and
whether that requires new (multi-node NVLink / GB200) hardware. An automated bot comment on
the PR asserted that standard AWS `p5` (H100) instances report GPU fabric state
`NOT_SUPPORTED`, and that fabric CI would need fabric-provisioned hardware plus the
`nvidia-imex` daemon.

This doc records what is actually true on the OSDC H100 fleet, verified against a live node
on 2026-07-24.

## TL;DR

- **OSDC H100 (`p5.48xlarge`) nodes are fabric-capable today.** `CU_MEM_HANDLE_TYPE_FABRIC`
  allocate → export → import → free was verified working on a live node. **No new hardware,
  no GB200 NVL72, no `nvidia-imex` daemon.**
- **The full production injection path is verified end-to-end** — a host-created channel plus
  `NVIDIA_IMEX_CHANNELS` on an *unprivileged* pod: the pod starts and the fabric probe passes.
- The bot's "p5 reports `NOT_SUPPORTED`" claim is **false** — a live p5 reports fabric
  `State: Completed / Status: Success`.
- The **only** missing piece is the **IMEX channel device**
  (`/dev/nvidia-caps-imex-channels/channel0`), which is absent on our nodes by design. It is
  a per-node/per-container config gap, not a hardware gap.

## Background: fd vs fabric handles

When a CUDA process shares a VMM allocation with another process, the exported handle is one
of two kinds:
- **POSIX fd** (`CU_MEM_HANDLE_TYPE_POSIX_FILE_DESCRIPTOR`) — works within a single machine.
  This is what every OSDC runner uses today.
- **Fabric** (`CU_MEM_HANDLE_TYPE_FABRIC`, a `CUmemFabricHandle`, CUDA 12.4+) — rides the
  NVLink fabric. PyTorch's expandable-segments IPC / SymmetricMemory selects this when
  `get_fabric_access()` is true.

The #190860 crash only occurs on the fabric branch: `unmapHandles` did
`close(std::get<int>(*h.shareable_handle))`, which throws from a destructor when the variant
holds a fabric handle (fixed by switching to the non-throwing `std::get_if<int>`).

## Finding: verified on a live node (2026-07-24)

Node `ip-10-8-105-26` (`p5.48xlarge`, 8× H100 80GB) on cluster `meta-prod-aws-uw1`, driver
`580.159.03`, CUDA `13.0`:

- `nvidia-smi -q` GPU Fabric: `State: Completed`, `Status: Success`, `CliqueId: 0`, all-zero
  `ClusterUUID` (a standalone single-node fabric island — not part of a multi-node clique).
- With an IMEX channel device present (`mknod .../channel0 c 242 0`, mode `0666`), a minimal
  CUDA driver-API program ran the full cycle successfully:
  `cuMemCreate(requestedHandleTypes=CU_MEM_HANDLE_TYPE_FABRIC)` →
  `cuMemExportToShareableHandle(FABRIC)` → `cuMemImportFromShareableHandle(FABRIC)` →
  `cuMemRelease` → exit 0 (`FABRIC_PROBE: PASS`).
- `CU_DEVICE_ATTRIBUTE_HANDLE_TYPE_FABRIC_SUPPORTED` returned 1 (informational only —
  documented-unreliable; the export/import round-trip is the authoritative test).

### Requirements for single-node fabric handles on H100
1. **NVSwitch node** — `p5` (H100) qualifies; it is an 8-GPU intra-node
   NVSwitch island.
2. **Fabric Manager running** — `nv-fabricmanager`; already running via the EKS NVIDIA AMI,
   which is why fabric `State` is `Completed`. FM configures NVSwitch/fabric; it does **not**
   create IMEX channels.
3. **IMEX channel device** — `/dev/nvidia-caps-imex-channels/channelN` (char major 242 on
   this AMI), created via `mknod` or the `NVreg_CreateImexChannel0=1` driver param, and
   accessible (`0666`) to the process. Required even single-node: the driver enforces channel
   access on `cuMemImportFromShareableHandle(FABRIC)` (else `CUDA_ERROR_NOT_PERMITTED`).
4. **NOT required single-node:** the `nvidia-imex` daemon (it only coordinates cross-node /
   multi-node NVLink domains).

### Current state on OSDC H100 nodes
- Fabric Manager: running. Fabric state: `Completed`.
- IMEX channel device: **absent** (not created by default; the node bootstrap deliberately
  does not create it).
- Net effect: `c10::cuda::get_fabric_access()` (PyTorch `c10/cuda/PeerToPeerAccess.cpp:164`)
  returns **false** — its `isFabricSupported()` probe's `cuMemImportFromShareableHandle(FABRIC)`
  fails with `CUDA_ERROR_NOT_PERMITTED` when no channel is accessible — so the allocator uses
  the POSIX-fd path. That is why no OSDC CI has ever hit the #190860 fabric-destructor bug.

## The documented Fabric Manager / IMEX race

`modules/nodepools-h100/scripts/h100-node-setup.sh:122-143`, added by osdc#552 (2026-05-12) after
osdc#541's eager approach caused a 100% H100 node-kill rate:

- **The race:** starting `nvidia-fabricmanager` from cloud-init races the GPUs' on-die
  "local FM" init. Host FM finishes NVSwitch routing in seconds, then waits up to the 30 s
  GFM Wait Timeout for the GPU local-FM instances to register over NVLink Inband. From
  cloud-init they are not ready yet → FM aborts with "config error type 8 / not all local
  fabric manager instances finished their configuration" → the hard-failing setup script
  fails → the node never joins → Karpenter reaps it.
- **The fix:** do nothing in cloud-init. The AMI already `systemctl enable`d FM, so systemd
  starts it later in normal boot (GPUs ready), completing in <1 s.
- **Scope:** the race is specific to **starting the FM daemon early**. Creating an IMEX
  channel device does not start FM and does not re-trigger this race.

**Correction to the script comment.** The comment states "IMEX channels are only needed for
multi-node NVLink." That is true for **NCCL** (intra-node NCCL/NVLS multicast uses POSIX-fd
handles) but **not** for CUDA `CU_MEM_HANDLE_TYPE_FABRIC` (PyTorch expandable-segments IPC /
SymmetricMemory), which needs the channel even on a single node. The comment also correctly
notes that `modprobe nvidia-caps-imex-channels` always fails — IMEX is compiled into
`nvidia.ko` (there is no separate `.ko`); the channel is created via `mknod` or the
`NVreg_CreateImexChannel0` module param, not by loading a module.

## Enabling fabric handles on the existing fleet (design)

No new hardware required. Two pieces:

### 1. Create the channel on the node
- **Runtime `mknod`** (fits ephemeral Karpenter nodes):
  `mknod /dev/nvidia-caps-imex-channels/channel0 c <major> 0 && chmod 0666`, deriving
  `<major>` from `/proc/devices` (242 on the current AMI). Add to `h100-node-setup.sh` after
  the `nvidia-smi -pm 1` line (:120). The script runs after the driver is loaded, so a
  `modprobe.d` drop-in would not take effect until reboot — hence runtime `mknod`, not
  modprobe, at cloud-init time.
- **Reboot-persistent alternative:** `options nvidia NVreg_CreateImexChannel0=1` in
  `/etc/modprobe.d/` + initramfs rebuild (auto-creates a world-accessible channel0 on every
  driver load). Heavier; for a baked AMI or fleet-wide rollout.
- Neither starts Fabric Manager, so neither re-triggers the documented race.

### 2. Inject the channel into runner job pods
- OSDC uses the **stock** `k8s-device-plugin` (`base/kubernetes/nvidia-device-plugin.yaml:45-48`,
  envvar strategy) with the NVIDIA container-toolkit runtime hook. Node toolkit config
  (`/etc/nvidia-container-runtime/config.toml`) has
  `accept-nvidia-visible-devices-envvar-when-unprivileged = true` (default) and CDI enabled,
  so **unprivileged** pods can request IMEX channels via env.
- Set `NVIDIA_IMEX_CHANNELS="0"` on the job container (the toolkit maps it to CDI
  `runtime.nvidia.com/imex-channel=0`). Equivalent alternatives: CDI device
  `nvidia.com/imex-channel=0`, or the `/dev/null → /var/run/nvidia-container-devices/imex/channel0`
  volume-mount.
- **Wire-up point:** the job-container env block in
  `modules/arc-runners/templates/runner.yaml.tpl:431-464`, driven by a new per-def field
  (e.g. `imex_channels`) read in `modules/arc-runners/scripts/python/generate_runners.py`
  (~:242, replacements dict ~:429-467) → a new `{{IMEX_ENV}}` placeholder. Set the field only
  on `modules/arc-runners-h100/defs/*.yaml` to gate to H100.

### FAILS CLOSED — ordering matters
If `NVIDIA_IMEX_CHANNELS` is set on a pod but the host channel does not exist, the toolkit
stat's the host device, fails, and the pod goes to **`StartError`**:
```
failed to apply edits for device "runtime.nvidia.com/imex-channel=0":
failed to stat CDI host device "/dev/nvidia-caps-imex-channels/channel0": no such file or directory
```
So channel creation on the node MUST land before/with the env injection, or **every** H100
runner pod fails to start. Conversely, creating the host channel is harmless to pods that do
NOT request it: injection is per-container, so their `get_fabric_access()` stays false and
their behavior is unchanged. This is what makes per-def gating safe.

## How this was verified (2026-07-24)

- A **self-contained privileged pod requesting all 8 GPUs** so it owns a whole node (no CI
  co-location). A 1-GPU request instead co-locates onto a busy CI node — avoid. An 8-GPU
  request routes to the `p5-large` Karpenter pool and scales a fresh `reserved` node.
- Image `nvcr.io/nvidia/cuda:13.0.0-devel-ubuntu24.04` (pulls via the node's Harbor
  `nvcr.io` mirror; `-devel` ships `nvcc`).
- The pod created the channel in-container (`mknod`), compiled a driver-API probe
  (`nvcc ... -L/usr/local/cuda/lib64/stubs -lcuda`), and ran it → `FABRIC_PROBE: PASS`.
- Isolation / scheduling facts: the `p5-48xlarge` pool is `consolidationPolicy: WhenEmpty`,
  `consolidateAfter: 1h`; fresh p5 nodes carry a `git-cache-not-ready:NoSchedule` startup
  taint (must be tolerated); a pod holding a non-DaemonSet workload pins its node against
  consolidation.

### Production injection path — verified (2026-07-24)
The unprivileged toolkit-injection path was confirmed end-to-end on an isolated `p5-large`
node (`ip-10-8-94-202`) with a single pod: a **privileged init container** `mknod`'d the host
channel (`crw-rw-rw- 242,0 channel0`), then the **unprivileged main container**
(`NVIDIA_IMEX_CHANNELS=0`, no privileged securityContext) **started** — no `StartError`, in
contrast to the bare attempt with no host channel — with
`/dev/nvidia-caps-imex-channels/channel0` injected by the toolkit, and the fabric probe
returned `FABRIC_PROBE: PASS`. This is the exact production wiring: node bootstrap creates the
host channel, the toolkit injects it into the unprivileged runner pod via the env, and the
unprivileged process uses fabric handles successfully. Caveat: the test container ran as
uid 0 (root) but *unprivileged* (no privileged securityContext); the channel's `0666` mode
makes it accessible to non-root runner UIDs (e.g. uid 1000) as well.

## Hardware scope
- **H100 (`p5.48xlarge`): verified.**
- **GB200 NVL72 (cross-node MNNVL):** a different regime that *does* need the `nvidia-imex`
  daemon; not present in OSDC and not required for single-node fabric handles. On AWS this is
  P6e-GB200 UltraServers (Capacity Blocks only) — overkill for exercising this code path.

## Key file pointers

OSDC (`ci-infra/osdc`):
- `modules/nodepools-h100/scripts/h100-node-setup.sh:122-143` — FM/IMEX "do nothing"
  rationale; `nvidia-smi -pm 1` at :120.
- `modules/arc-runners/templates/runner.yaml.tpl:431-464` — job-container env block
  (injection point).
- `modules/arc-runners/scripts/python/generate_runners.py` — ~:242 (read def field),
  :359-378 (GPU tolerations/affinity/requests logic), ~:429-467 (replacements dict).
- `base/kubernetes/nvidia-device-plugin.yaml:45-48` — stock device plugin, no IMEX config.
- `modules/nodepools-h100/defs/{p5,p5-large}.yaml`, `modules/arc-runners-h100/defs/*.yaml`.

PyTorch (`pytorch/pytorch`):
- `c10/cuda/PeerToPeerAccess.cpp:164` (`get_fabric_access`), :110 (`isFabricSupported`).
- `c10/cuda/CUDACachingAllocator.cpp:826` (`detectHandleType`), :620-638 (`share`), :942
  (destructor `std::get<int>` fixed by #190860), :993 (`shareable_handle` variant).
- `test/distributed/test_p2p_ipc.py::test_p2p_ipc_expandable_segments` (currently
  `@unittest.skip`, #189879).
- PR: https://github.com/pytorch/pytorch/pull/190860

## Sources (NVIDIA)
- CUDA VMM — fabric handles single- and multi-node:
  https://docs.nvidia.com/cuda/cuda-programming-guide/04-special-topics/virtual-memory-management.html
- CUDA Driver API — `CUDA_ERROR_NOT_PERMITTED` on fabric import:
  https://docs.nvidia.com/cuda/cuda-driver-api/group__CUDA__VA.html
- IMEX channels guide: https://docs.nvidia.com/multi-node-nvlink-systems/imex-guide/imexchannels.html
- IMEX overview (daemon = multi-node only):
  https://docs.nvidia.com/multi-node-nvlink-systems/imex-guide/overview.html
- Fabric Manager user guide (single-node HGX/DGX scope):
  https://docs.nvidia.com/datacenter/tesla/fabric-manager-user-guide/index.html
- NVML fabric-state defs: https://docs.nvidia.com/deploy/nvml-api/group__nvmlFabricDefs.html
- Container Toolkit CDI / IMEX:
  https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/cdi-support.html
- Single-node fabric-handle confirmation (NVIDIA staff, dev forum):
  https://forums.developer.nvidia.com/t/cudevicegetattribute-shows-i-can-use-fabric-handle-but-actually-i-cannot/336426

## Source / provenance
Verified on `meta-prod-aws-uw1` (`p5.48xlarge`, driver 580.159.03, CUDA 13.0) on 2026-07-24
via two isolated probe pods: a self-contained privileged pod that created the channel
in-container and ran the fabric probe (node `ip-10-8-105-26`), and a two-container pod that
created the channel on the host and ran the probe from an unprivileged container with the
channel toolkit-injected via `NVIDIA_IMEX_CHANNELS` (node `ip-10-8-94-202`) — both
`FABRIC_PROBE: PASS`. Plus source review of OSDC node/runner modules and PyTorch `c10/cuda`.
Prompted by pytorch/pytorch#190860.
