# Moving the NVIDIA driver ahead of the AWS AMI schedule

Status: **investigation, no decision made.** Captured 2026-09-10 so we can pick it
up later. Trigger was a question about CUDA 13.4 support on the A10G / L4 runners.

## The gap

Every GPU fleet takes its driver from the stock EKS NVIDIA AMI. There is no
driver pin anywhere in `osdc/` — `generate_nodepools.py` selects GPU AMIs with a
name glob and the driver is whatever that image happens to carry:

```python
ami_selector_block = """  amiSelectorTerms:
    - name: "amazon-eks-node-al2023-x86_64-nvidia-*\""""
```

This covers **all** GPU fleets — g5 (A10G), g6 (L4), g4dn (T4), p4d, p5 (H100),
p6-b200. `ai-sandbox` is the only def in the repo that overrides the AMI, and it
is CPU-only.

Consequence: the driver moves when AWS decides it moves, and we have no lever.

## Current state (verified 2026-09-10)

| | |
|---|---|
| Driver | `580.178.04` (`kmod-nvidia-latest-dkms-580.178.04-1.amzn2023`) |
| Driver branch | **R580** |
| nvidia-container-toolkit | `1.20.0` |
| AMI | `amazon-eks-node-al2023-x86_64-nvidia-{1.35,1.36}-v20260903` |

AWS driver history across recent EKS AMI releases:

| AMI release | Driver |
|---|---|
| v20260903 | 580.178.04 |
| v20260827 | 580.178.04 |
| v20260818 | 580.178.04 |
| v20260810 | 580.159.03 |
| v20260801 | 580.159.03 |
| v20260728 | 580.159.03 |

AWS has stayed on R580 throughout. They have not shipped R590, R595, R610 or
R615 in any EKS AMI to date.

## Why this matters: CUDA 13.x is branch-gated

The CUDA 13.4 release notes map toolkits to *driver branches*, not to point
releases the way 12.9 and earlier did:

| CUDA | Driver branch |
|---|---|
| 13.4 | **R615** |
| 13.3 | R610 |
| 13.2 | R595 |
| 13.1 | R590 |
| 13.0 | R580 |

Two statements from those notes set the boundary:

> Existing CUDA 13.x applications run on drivers >=580 under CUDA minor version
> compatibility.

> CUDA 13.4 new features and newly enabled platforms require an R615 or later
> driver that supports them.

So R580 clears the `>= 580` minor-compat floor — 13.4-built code that stays on
13.0-era driver APIs runs today. Anything using actual 13.4 features does not,
and R615 is four branches beyond what AWS ships.

Note CUDA 13.4 is currently a **Developer Preview**, not GA, and NVIDIA labels it
unfit for production. Nothing is urgent yet. Two packaging changes land with it
that affect any build we do ourselves: on Linux the driver stops shipping with
the toolkit as of 13.4, and R615 packages no longer include the proprietary
kernel modules.

## Options

### 1. Custom GPU AMI (leading candidate)

Build on the stock EKS NVIDIA AMI and upgrade the driver, then select it by tag.
The machinery already exists and is proven: `modules/nodepools-agent-sandbox/packer`
does exactly this shape for gVisor, and the nodepool generator already supports
`ami_selector_tags` per def.

What we take on:

- **CVE fixes stop being automatic.** This is the real cost. The glob currently
  picks up kernel fixes on node rotation with no action — see the base node
  groups in `clusters.yaml`, which were the *only* nodes left unpatched for
  CVE-2026-64561 precisely because they were pinned. A custom GPU AMI puts every
  GPU node in that same category, and GPU nodes are the bulk of the fleet.
- **We own EKS version skew.** The glob has no Kubernetes version in it, which is
  already causing drift worth fixing independently (see below). A tag selector
  has the same blind spot — the `ai-sandbox` def carries a TODO about exactly
  this.
- Per-region builds, since AMIs are regional.

Worth scoping a rebuild-on-a-schedule job if we go this way, so the image does
not silently rot.

### 2. `cuda-compat-13-4` in the CI container image

Ship the forward-compatibility package in the image rather than touching nodes at
all. Confirmed viable for our hardware: it requires a base driver `>= r580`
(we have 580.178.04) and is supported on NVIDIA Data Center GPUs, which covers
A10G, L4, T4, A100, H100 and B200.

It installs driver-615 libraries under `/usr/local/cuda-13.4/compat/` and only
provides the libraries — the consumer sets `LD_LIBRARY_PATH`. CUDA/OpenGL and
CUDA/Vulkan interop are not supported through it.

Cheapest option by a wide margin and it changes no infrastructure, but it is
per-image rather than fleet-wide, so it only helps workloads we control.

### 3. NVIDIA GPU Operator driver containers

Fleet-wide and decoupled from the AMI, but it has to displace the driver already
baked into the EKS NVIDIA AMI, which is the awkward part. Heaviest of the three.

## Open questions

- Do we actually need R615 features, or is minor-version compatibility enough?
  This decides whether any of this is worth doing. Nothing has demanded 13.4 yet.
- If we build a custom AMI, what keeps it current on kernel CVEs?
- Does AWS have a public position on when EKS AMIs move off R580?

## Unrelated issue found while investigating

The GPU AMI glob has no Kubernetes version in it, so it matches the newest NVIDIA
AMI of *any* minor. In us-east-2 and us-west-1 that is now the 1.36 image, while
the control planes are 1.35:

| Cluster | Control plane | GPU kubelet | CPU kubelet |
|---|---|---|---|
| meta-prod-aws-ue1 | v1.35.6 | v1.35.7 | v1.35.7 |
| meta-prod-aws-ue2 | v1.35.6 | **v1.36.3** | v1.35.7 |

A kubelet newer than the API server is outside the supported skew. The CPU path
does not have this problem because `alias: al2023@latest` tracks the cluster's
Kubernetes version. Fixing the glob to include `eks_version` is worth doing
regardless of what we decide about drivers.

## Verifying current state

```bash
# Driver the fleet is actually running
kubectl --context <cluster> get nodes -l node-fleet=g5 \
  -o jsonpath='{.items[0].spec.providerID}' | sed 's|.*/||'
aws ec2 describe-instances --region <region> --instance-ids <id> \
  --query 'Reservations[].Instances[].ImageId' --output text
# then look up that AMI name in the amazon-eks-ami release notes

# Node-level, from the tuning DaemonSet's own output
nvidia-smi --query-gpu=name,driver_version --format=csv,noheader
```

## References

- [CUDA 13.4 Developer Preview release notes](https://docs.nvidia.com/cuda/developer-preview/13.4/cuda-toolkit-release-notes/index.html) — branch table, R615 requirement
- [CUDA forward compatibility](https://docs.nvidia.com/deploy/cuda-compatibility/forward-compatibility.html) — `cuda-compat` hardware and driver-branch limits
- [amazon-eks-ami releases](https://github.com/awslabs/amazon-eks-ami/releases) — per-release driver versions
- `docs/h100-fabric-handles-imex-channels.md` — records 580.159.03 / CUDA 13.0 from a July 2026 verification; superseded by 580.178.04
