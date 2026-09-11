# NVIDIA driver R615 for the compute GPU fleets (CUDA 13.4)

Investigation and a parked implementation. **Nothing is adopted** — every GPU
pool still runs the stock EKS GPU AMI at driver 580. This records what CUDA 13.4
actually requires, why the obvious upgrade paths do not work, and what the build
in `modules/nodepools/packer/` does if we decide we need it.

Verified September 2026 against CUDA 13.4.1 GA and EKS AMI `v20260903`.

> **Companion doc:** `docs/nvidia-driver-ahead-of-aws.md` (PR #1064) is the
> broader survey — observed driver versions across recent AMI releases, the
> `cuda-compat-13-4` and GPU Operator options, and the separate GPU-AMI-glob
> Kubernetes skew bug it turned up. This doc is narrower: it is the build for
> that survey's leading candidate, plus the two things that only surface once
> you try it (the GRID runfile ceiling, and the boot-time kmod selection
> failure). Read that one first for the decision, this one for the mechanics.
> Note 13.4 has since gone GA, which that doc predates.

## What CUDA 13.4 actually requires

CUDA 13.4.1 went GA in September 2026 (13.4.0 was a July developer preview). Its
corresponding driver branch is **R615** — `615.71.09` on Linux.

The distinction that decides whether any of this is worth doing:

| Need | Minimum driver |
|------|----------------|
| Run existing CUDA 13.x applications (minor version compatibility) | **>= 580** |
| CUDA 13.4's *new* features and newly enabled platforms | **R615** |

The fleet is already at 580. **CUDA 13.4 binaries run on it today.** R615 buys
13.4-specific features — Vera Rubin support (developer preview), PTX ISA 9.4,
GCC 16 / Clang 22 / libc++ 22 with nvcc, Windows-on-Arm — none of which is on
the critical path for PyTorch CI as of this writing. `pypi-cache` currently
builds `cu126`, `cu128` and `cu130` (`clusters.yaml`), so nothing in the fleet
targets 13.4 yet either.

If the only goal is "run CUDA 13.4 workloads", the correct amount of
infrastructure work is **zero**, plus a `cu134` slug in pypi-cache.

## Where the driver comes from today

Nothing in `osdc/` pins a driver version. GPU nodepools take whatever the newest
EKS GPU AMI ships:

```python
# modules/nodepools/scripts/python/generate_nodepools.py
if is_gpu:
    ami_family_block = "  amiFamily: AL2023"
    ami_selector_block = """  amiSelectorTerms:
    - name: "amazon-eks-node-al2023-x86_64-nvidia-*\""""
```

That glob covers g5/g6/g4dn, p4d, p5 (`nodepools-h100`) and p6-b200
(`nodepools-b200`). `base_node_ami_version: "v20260903"` in `clusters.yaml` pins
only the CPU `-standard-` image for base nodes. The H100 and B200 userData
scripts do persistence mode and IMEX channel setup but explicitly rely on the
AMI for the driver and Fabric Manager.

Current resolution, us-east-2 / k8s 1.35: `ami-03d982742c1ceed36`, release
`1.35.7-20260903`, **driver 580**.

(For contrast, `gpu-dev/` does pin — it `dnf install`s the driver directly in
`terraform-gpu-devservers/templates/al2023-user-data.sh`, currently landing on
595. That fleet could move to 615 with a one-line change and no AMI build, which
makes it the cheaper place to get a first real signal on R615.)

## Why there is no easy path to 615

Three doors, all closed:

**1. Wait for the stock AMI.** The EKS AMI build defaults to
`nvidia_driver_major_version = "580"`. Upstream added *build* support for 595
recently, but 595 is not the published default, and there is no 610 or 615 work
in `awslabs/amazon-eks-ami` at all.

**2. Build the EKS AMI from source with `=615`.** Two hard blockers:

- The build resolves the full version as `min(kmod-nvidia-open-dkms, AWS GRID
  runfile)`. AWS's public `s3://ec2-linux-nvidia-drivers` bucket tops out at
  `595.91.07` — no 610, no 615. `archive-grid-kmod` hard-errors when no runfile
  matches, and the `min()` would pin back to 595 regardless.
- `archive-proprietary-kmod` installs `kmod-nvidia-latest-dkms-<version>`.
  **R615 ships no proprietary kernel module.** The NVIDIA AL2023 repo's
  `kmod-nvidia-latest-dkms` stops at 610; from 615 the RPMs obsolete
  `cuda-drivers` in favour of `nvidia-open`.

  This is also why AWS cannot trivially ship 615 themselves: their boot-time
  flavor selector hardcodes `g4dn`/`g5`/`g5g` to the proprietary module for a
  GSP workaround.

**3. NVIDIA GPU Operator managing the driver.** Not supported on Amazon Linux —
AWS's own guidance is to run the operator with `driver.enabled=false` on the
accelerated AMIs, and NVIDIA publishes no AL2023 precompiled driver container.
It would mean moving the GPU fleets to self-managed Ubuntu nodes.

**What is available:** the NVIDIA `cuda-amzn2023` repo has the complete 615.71.09
set — `kmod-nvidia-open-dkms`, `nvidia-driver*`, `nvidia-fabricmanager`,
`nvidia-imex`, `nvidia-persistenced`, and `cuda-compat-13-4`. So the packages
exist; only AWS's packaging of them does not.

## The fourth door, which is what got built

Take the finished EKS GPU AMI and swap the driver in place with Packer. Keeps us
on AWS's image, confines the delta to one reviewable script, and reuses the
`ami_selector_tags` escape hatch and Packer pattern the repo already has from
`nodepools-agent-sandbox`.

`modules/nodepools/packer/` — see its README for the mechanics (DKMS archives,
the supported-devices list, and the two patches to
`/etc/eks/nvidia-kmod-load.sh` that are required because upstream's flavor
selection assumes a proprietary kmod exists).

```bash
just build-gpu-driver-ami meta-prod-aws-ue2
```

Building changes no running fleet. Adoption is a per-def opt-in:

```yaml
  ami_selector_tags:
    osdc.io/ami: gpu-nvidia-615
```

## Risk notes

Two R615 items that sound alarming and are not, here:

- **CDMM.** R615 defaults to driver-managed GPU memory instead of onlining it as
  a NUMA node, on hardware-coherent platforms. That is GB200-class hardware;
  every OSDC GPU node is an x86 host with PCIe/NVLink GPUs. It does not apply —
  and `nvidia-kmod-load.sh` already writes
  `NVreg_CoherentGPUMemoryMode=driver` unconditionally today.
- **The 610→615 in-place upgrade hang.** Karpenter nodes are born at the target
  version; there is no in-place upgrade.

Two that are real:

- **g4dn/g5/g6 are out of scope.** They are exactly the pools that depend on the
  proprietary kmod and the GSP workaround. The AMI carries a fallback so such a
  node boots on the open kmod rather than breaking, but it is untested. Keep
  those pools on the stock glob.
- **The AMI stops tracking CVE fixes.** Stock GPU pools pick up AL2023 and
  driver fixes automatically on node rotation. A fleet pinned to a baked AMI
  only moves when the AMI is rebuilt, and nothing enforces a rebuild on an EKS
  version bump. This is the strongest argument for not adopting until there is a
  concrete need.

## If we adopt

Suggested order, smallest blast radius first:

1. `p6-b200` — one node, single capacity block `cr-0b15c0b3163f09d26` in
   `meta-prod-aws-ue2`. Newest silicon, most to gain.
2. `p5` — but note `h100-node-setup.sh` builds IMEX channels, which is the most
   driver-sensitive thing in the fleet. Validate fabric handles per
   `docs/h100-fabric-handles-imex-channels.md` before trusting it.
3. `p4d` — on-demand, no capacity block, so the easiest to revert.

Per-node validation after the first boot: `nvidia-smi` reports 615.71.09;
`nvidia-fabricmanager` and `nvidia-persistenced` are active; `dcgm-exporter` is
scraping; `lsmod | grep nvidia` shows the open module; `gdrdrv` loaded and
`/dev/gdrdrv` present; and a real GPU job passes end to end.

## Sources

- [CUDA Toolkit 13.4 release notes](https://docs.nvidia.com/cuda/cuda-toolkit-release-notes/index.html)
- [NVIDIA Data Center Driver 615.71.09 release notes](https://docs.nvidia.com/datacenter/tesla/tesla-release-notes-615-71-09/index.html)
- [EKS-optimized accelerated AMIs](https://docs.aws.amazon.com/eks/latest/userguide/ml-eks-optimized-ami.html)
- [awslabs/amazon-eks-ami#2768](https://github.com/awslabs/amazon-eks-ami/issues/2768) — the g7 open-vs-proprietary selection bug this AMI's patch also fixes
