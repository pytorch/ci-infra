# nodepools/packer — compute-GPU AMI with NVIDIA 615

Builds an EKS AL2023 GPU AMI with the NVIDIA driver swapped from the stock **580**
to **615.71.09** (R615), the branch CUDA 13.4 asks for.

**Nothing selects this AMI.** Every GPU pool still tracks the stock
`amazon-eks-node-al2023-x86_64-nvidia-*` glob. This is built and parked so the
option exists; adopting it is a separate, one-line change per def. The *why* —
including why 580 is very likely good enough — is in
[`docs/nvidia-driver-615-ami.md`](../../../docs/nvidia-driver-615-ami.md).

Lives under `modules/nodepools/` rather than a shim because `nodepools-h100` and
`nodepools-b200` both delegate here, so one AMI serves p4d, p5 and p6-b200.

## Build

```bash
just build-gpu-driver-ami <cluster>                              # e.g. meta-prod-aws-ue2
just build-gpu-driver-ami <cluster> -var nvidia_driver_version=615.71.09
```

AMIs are regional — build once per cluster region. The build instance is a
`c7a.4xlarge`, not a p-family box: DKMS compiles the module without a GPU, so a
build costs cents rather than dollars.

## Why a swap and not a build flag

The upstream EKS AMI build takes `nvidia_driver_major_version`, so `=615` looks
like it should just work. It does not, for two independent reasons:

- `install-nvidia-driver.sh` resolves the full version as
  `min(kmod-nvidia-open-dkms, AWS GRID runfile)`, and AWS's
  `s3://ec2-linux-nvidia-drivers` bucket stops at **595.91.07**. There is no 615
  runfile, so the build either errors or silently pins back to 595.
- `archive-proprietary-kmod` installs `kmod-nvidia-latest-dkms-<version>`.
  **R615 ships no proprietary kernel module** — the NVIDIA AL2023 repo's
  proprietary packages stop at 610 — so that step cannot succeed.

Both are fixable, but only by forking the upstream template. Swapping the driver
on the finished AMI keeps us on AWS's image and confines the delta to one script.

## What the swap touches

Following the same sequence as upstream's `install-nvidia-driver.sh`:

1. **DKMS archives** under `/var/lib/dkms-archive/` — this is what
   `nvidia-kmod-load.service` unpacks at boot, so it is the thing that actually
   determines a node's driver. `nvidia-open` and `gdrdrv` are rebuilt at 615;
   `nvidia` (proprietary) and `nvidia-open-grid` are deleted and not replaced,
   because neither exists for this branch.
2. **Userspace RPMs** — driver, CUDA libs, Fabric Manager, IMEX, persistenced,
   all pinned to the same full version. A closing assertion fails the build if
   dnf left any `nvidia-*` package behind at 580.
3. **`/etc/eks/nvidia-open-supported-devices-615.txt`** — generated from the
   `supported-gpus.json` the `nvidia-driver` RPM ships, using upstream's own jq
   filter. Upstream only publishes lists up to 595. The build asserts that A100
   (`0x20B2`), H100 (`0x2330`) and B200 (`0x2901`) are in it.
4. **`/etc/eks/nvidia-kmod-load.sh`** — two patches, described below.

## The two patches to nvidia-kmod-load.sh

Both are anchored on exact upstream text, and **the build fails if an anchor is
missing**. A base-AMI refresh that rewrites this script should be reviewed, not
silently shipped.

**The version probe.** Upstream reads the major version from the proprietary
kmod:

```bash
KMOD_MAJOR_VERSION=$(rpmquery kmod-nvidia-latest-dkms --queryformat '%{VERSION}' | cut -d. -f1)
```

That package does not exist here, so the probe fails, `devices-support-open`
returns 1, and selection falls through to a proprietary flavor that also does not
exist — a node with no driver loaded at all. It is replaced with a read of
`/etc/eks/osdc-nvidia-driver-major`, written at bake time. (This is the same
shape as the g7 bug in [awslabs/amazon-eks-ami#2768](https://github.com/awslabs/amazon-eks-ami/issues/2768).)

**The flavor fallback.** `g4dn`/`g5`/`g5g` are hardcoded to the proprietary
module for a GSP workaround, and GRID subdevices select `nvidia-open-grid`. A
guard before `kmod-util load` redirects any selection whose archive is absent to
`nvidia-open`, and removes `nvidia-disable-gsp.conf` on the way (the open kmod
*requires* GSP firmware, so `NVreg_EnableGpuFirmware=0` must not outlive the
proprietary flavor that set it).

The guard means a g5 node would boot rather than break, but **that path is
untested** — see scope below.

## Scope: compute GPUs only

Intended for `p4d` (A100), `p5` (H100), `p6-b200` (B200). Not validated on
`g4dn`/`g5`/`g6`, which are the pools that actually depend on the proprietary
kmod and the GSP workaround upstream added for them.

Two things that are *not* a concern here, both worth knowing:

- **CDMM.** R615 changes the default to driver-managed GPU memory on
  hardware-coherent platforms. Every OSDC GPU node is an x86 host with PCIe /
  NVLink-attached GPUs, so it does not apply — and the EKS AMI already writes
  `NVreg_CoherentGPUMemoryMode=driver` unconditionally regardless.
- **The 610→615 upgrade hang.** NVIDIA advises a reboot after upgrading in
  place. Karpenter nodes are born at the target version, so there is no in-place
  upgrade to survive.

## Adopting it

One block per def, e.g. `modules/nodepools-b200/defs/p6.yaml`:

```yaml
  ami_selector_tags:
    osdc.io/ami: gpu-nvidia-615
```

The generator (`generate_nodepools.py`) turns that into an EC2NodeClass
`amiSelectorTerms.tags` and drops the name glob. Roll back by deleting the block
and redeploying — existing nodes keep their AMI until recycled
(`just recycle-nodes <cluster>`).

Start with `p6-b200`: one node behind a single capacity block in
`meta-prod-aws-ue2`, the smallest blast radius of the three, and the Blackwell
part with the most to gain from a newer driver.

## Rebuild cadence

Stock GPU pools track the AMI glob and pick up AL2023 and driver CVE fixes
automatically on node rotation. **A fleet on this AMI does not** — it pins
whatever EKS GPU image was current at build time, and only moves when rebuilt.
Each rebuild re-resolves the base from the EKS SSM parameter, so rebuilding is
the whole refresh procedure. An EKS version bump needs one too, and nothing
enforces it at launch: the def selects on `osdc.io/ami` alone, so `K8sVersion` in
the tags is recorded but ignored.

That ongoing cost is the main argument for waiting for AWS to ship 615 instead.
