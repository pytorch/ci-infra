# nodepools/packer — compute-GPU AMI on NVIDIA R615

Builds an EKS AL2023 GPU AMI on NVIDIA **615.71.09** (R615), the branch CUDA 13.4
asks for, instead of the **580** AWS publishes.

**Nothing selects this AMI.** Every GPU pool still tracks the stock
`amazon-eks-node-al2023-x86_64-nvidia-*` glob. This is built and parked so the
option exists; adopting it is a separate, one-line change per def. The *why* —
including why 580 is very likely good enough — is in
[`docs/nvidia-driver-615-ami.md`](../../../docs/nvidia-driver-615-ami.md).

Lives under `modules/nodepools/` rather than a shim because `nodepools-h100` and
`nodepools-b200` both delegate here, so one AMI serves p4d, p5 and p6-b200.

## How it works

Upstream's own AMI build, at a pinned tag, with two patches:

```
build-gpu-ami.sh          clone upstream @ tag -> git apply patches/ -> make -> tag the AMI
patches/0001-…            build only the open kmod on branches that ship only the open kmod
patches/0002-…            stop the boot-time selector depending on the proprietary kmod's RPM
```

The patches are the entire delta. They are written to be legible as an upstream
contribution rather than as local hacks — both are gated on
`NVIDIA_DRIVER_MAJOR_VERSION >= 615`, so they are no-ops on every branch AWS
currently builds.

```bash
just build-gpu-driver-ami <cluster>                     # e.g. meta-prod-aws-ue2
UPSTREAM_REF=v20261015 just build-gpu-driver-ami <cluster>
```

AMIs are regional — build once per cluster region.

## Why upstream can't just be pointed at 615

Passing `nvidia_driver_major_version=615` to an unpatched build fails, for one
reason that is real and one that looks real and isn't:

**Not a blocker: the GRID runfile.** Upstream resolves the driver version as
`min(kmod-nvidia-open-dkms, AWS GRID runfile)`, and AWS's
`s3://ec2-linux-nvidia-drivers` bucket stops at `595.91.07`. That reads like a
wall, but `nvidia_grid_runfile_bucket_name` is an ordinary Packer variable —
mirroring the bucket ourselves is supported configuration. We don't, because a
compute-only image never selects the GRID flavor, so patch 0001 drops the
lookup instead. Sourcing a *genuine* GRID 615 would mean the NVIDIA vGPU
licensing path; renaming a Tesla runfile would leave an archive labelled
`nvidia-open-grid` holding a non-GRID driver.

**The actual blocker: the proprietary kmod.** `archive-proprietary-kmod` runs
unconditionally and installs `kmod-nvidia-latest-dkms-<version>`. NVIDIA stopped
building it at 610:

| | present in the NVIDIA AL2023 repo |
|---|---|
| `kmod-nvidia-open-dkms` | …595.91.07, 610.43.02, 610.57.04, **615.71.09** |
| `kmod-nvidia-latest-dkms` | …595.91.07, 610.43.02, 610.57.04 — **none at 615** |

No bucket helps — it is a dnf package, not an S3 object.

## Patch 0002 is not optional

`archive-proprietary-kmod` is also what leaves the `kmod-nvidia-latest-dkms`
**RPM** installed on the finished image: it archives the module and
`kmod-util remove`s it, but never `dnf remove`s the package. And
`nvidia-kmod-load.sh` reads the driver's major version from exactly that RPM at
boot:

```bash
KMOD_MAJOR_VERSION=$(rpmquery kmod-nvidia-latest-dkms --queryformat '%{VERSION}' | cut -d. -f1)
```

So the moment patch 0001 stops archiving the proprietary flavor, that probe
fails, `devices-support-open` returns 1, and selection falls through to a flavor
the image does not carry — **a node that boots with no driver loaded at all**.
Same shape as
[awslabs/amazon-eks-ami#2768](https://github.com/awslabs/amazon-eks-ami/issues/2768).

Patch 0002 fixes it in two places: the build records the major version to
`/etc/eks/nvidia-driver-major` and the probe prefers that file (falling back to
the RPM, so the patch is a no-op on stock builds), and a guard before
`kmod-util load` redirects any flavor whose archive is missing to `nvidia-open`,
removing `nvidia-disable-gsp.conf` on the way — the open kmod requires GSP
firmware, so a proprietary-only workaround must not outlive its flavor.

## Bumping the pinned tag

`UPSTREAM_REF` in `build-gpu-ami.sh` and the patch context lines are a matched
pair. `git apply` runs without `--3way` and without fuzz on purpose: if a patch
stops applying, upstream's logic moved and that needs a human. Regenerate with

```bash
git clone --depth 1 --branch <tag> https://github.com/awslabs/amazon-eks-ami.git
# edit, then:
git diff -- templates/al2023/provisioners/install-nvidia-driver.sh > patches/0001-…
git diff -- templates/al2023/runtime/gpu/nvidia-kmod-load.sh      > patches/0002-…
```

## Scope: compute GPUs only

Intended for `p4d` (A100), `p5` (H100), `p6-b200` (B200). Not validated on
`g4dn`/`g5`/`g6`, which are the pools that actually depend on the proprietary
kmod and the GSP workaround upstream added for them. Patch 0002's guard means
such a node boots rather than breaks, but that path is untested.

Two things that are *not* a concern here:

- **CDMM.** R615 changes the default to driver-managed GPU memory on
  hardware-coherent platforms. Every OSDC GPU node is an x86 host with PCIe /
  NVLink-attached GPUs, so it does not apply — and `nvidia-kmod-load.sh` already
  writes `NVreg_CoherentGPUMemoryMode=driver` unconditionally regardless.
- **The 610→615 upgrade hang.** NVIDIA advises a reboot after upgrading in
  place. Karpenter nodes are born at the target version.

One thing that is: upstream sets `http_tokens = "required"` for the build
instance but does not set `imds_support = "v2.0"` on the resulting AMI, unlike
`modules/nodepools-agent-sandbox/packer`. That matches the stock GPU AMI, so
this is not a regression, but it is a difference worth knowing.

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
`meta-prod-aws-ue2`, the smallest blast radius of the three.

## Rebuild cadence

Stock GPU pools track the AMI glob and pick up AL2023 and driver CVE fixes
automatically on node rotation. **A fleet on this AMI does not** — it pins the
upstream tag and the base image that tag resolves, and only moves when rebuilt.
An EKS version bump needs a rebuild too, and nothing enforces it at launch: the
def selects on `osdc.io/ami` alone, so `K8sVersion` in the tags is recorded but
ignored.

That ongoing cost is the main argument for waiting for AWS to ship 615 instead.
