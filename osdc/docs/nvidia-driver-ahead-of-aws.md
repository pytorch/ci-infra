# Moving the NVIDIA driver ahead of the AWS AMI schedule

Status: **investigation, no decision made.** Captured 2026-09-10, refreshed
2026-09-14. Trigger was a question about CUDA 13.4 support on the A10G / L4
runners.

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

**CUDA 13.4 is now GA** — 13.4.1 shipped in September 2026, superseding the
13.4.0 Developer Preview this doc originally recorded as "not fit for
production". That removes the "nothing is urgent because it is a preview"
argument, but not the conclusion: minor version compatibility still means R580
runs 13.4 binaries, so the question is still whether we need 13.4's *features*,
and nothing has demanded them yet.

Two packaging changes land with 13.4 that affect any build we do ourselves: on
Linux the driver stops shipping with the toolkit as of 13.4, and R615 packages no
longer include the proprietary kernel modules. That second one has a sharper edge
than it first appears — see option 1 below.

## Options

### 1. Custom GPU AMI (leading candidate)

Build on the stock EKS NVIDIA AMI and upgrade the driver, then select it by tag.
The machinery already exists and is proven: `modules/nodepools-agent-sandbox/packer`
does exactly this shape for gVisor, and the nodepool generator already supports
`ami_selector_tags` per def.

**This has since been built and parked** — see PR #1068 and
`docs/nvidia-driver-615-ami.md`. Building it turned up two things that are not
obvious from the outside and that rule out the simpler variants:

- **Building the EKS AMI from source with `nvidia_driver_major_version=615` does
  not work.** The upstream build resolves the version as
  `min(kmod-nvidia-open-dkms, AWS GRID runfile)`, and AWS's public
  `s3://ec2-linux-nvidia-drivers` bucket stops at `595.91.07`. There is no 610 or
  615 runfile, so the build either hard-errors or silently pins back to 595. A
  swap on the finished AMI is the only route that reaches 615.
- **R615 dropping the proprietary kmod breaks node boot, not just packaging.**
  `/etc/eks/nvidia-kmod-load.sh` probes the driver's major version with
  `rpmquery kmod-nvidia-latest-dkms` — the proprietary package, absent at 615 —
  so the probe fails, the open-kmod check returns false, and selection falls
  through to a flavor that does not exist. The node comes up with no driver
  loaded at all. Same shape as
  [awslabs/amazon-eks-ami#2768](https://github.com/awslabs/amazon-eks-ami/issues/2768).
  It also means `g4dn`/`g5`/`g5g` cannot use a 615 AMI without further patching:
  upstream hardcodes them to the proprietary module for a GSP workaround.

What we take on:

- **CVE fixes stop being automatic.** This is the real cost. The glob currently
  picks up kernel fixes on node rotation with no action — see the base node
  groups in `clusters.yaml`, which were the *only* nodes left unpatched for
  [CVE-2026-64561](https://explore.alas.aws.amazon.com/CVE-2026-64561.html)
  ("Zapscape", fixed by the `base_node_ami_version: "v20260903"` bump in #1062)
  precisely because they were pinned. A custom GPU AMI puts every GPU node in
  that same category, and GPU nodes are the bulk of the fleet.
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
  This decides whether any of this is worth doing. Nothing has demanded 13.4 yet,
  and `pypi-cache` still tops out at `cu130`.
- If we build a custom AMI, what keeps it current on kernel CVEs?
- ~~Does AWS have a public position on when EKS AMIs move off R580?~~ Partial
  answer: upstream has landed *build* support for 595
  ([#2747](https://github.com/awslabs/amazon-eks-ami/pull/2747)) but has not made
  it the published default, and AWS's own guidance for the G7 family — which
  needs 595 — is to build a custom AMI rather than wait. There is no 610 or 615
  work upstream at all, and their GRID runfile bucket would gate it regardless.
  Read that as: not soon.

## Separate bug found while investigating: GPU kubelet version skew

Not about drivers, but it comes from the same glob, so it is recorded here until
it has a home in the tracker.

The GPU AMI glob has no Kubernetes version in it, so it matches the newest NVIDIA
AMI of *any* minor. Measured live 2026-09-14, every meta-prod cluster is on a
v1.35.6 control plane:

| Cluster | Region | GPU kubelet | CPU kubelet | GPU nodes affected |
|---|---|---|---|---|
| meta-prod-aws-ue1 | us-east-1 | v1.35.7 | v1.35.7 | 0 of 287 |
| meta-prod-aws-ue2 | us-east-2 | **v1.36.3** | v1.35.7 | **560** |
| meta-prod-aws-uw1 | us-west-1 | **v1.36.3** | v1.35.7 | **2** (both p5/H100) |

A kubelet newer than the API server is outside the supported skew, so that is
562 production GPU nodes in an unsupported configuration today. The CPU path is
unaffected because `alias: al2023@latest` tracks the cluster's Kubernetes
version.

**us-east-1 is not safe, it is lucky.** The glob resolves to whichever matching
AMI is newest, and AWS registers all minors of a release within a few seconds:

| Region | Newest match | Registered | Runner-up |
|---|---|---|---|
| us-east-1 | `…nvidia-1.35-v20260903` | 22:43:17 | `…-1.36-` at 22:43:16 |
| us-east-2 | `…nvidia-1.36-v20260903` | 22:43:26 | `…-1.35-` at 22:43:25 |
| us-west-1 | `…nvidia-1.36-v20260903` | 22:41:55 | `…-1.35-` at 22:41:53 |

us-east-1 escapes by **one second** of registration ordering, and could flip on
any future AMI release. Any cluster in us-east-2 or us-west-1 on a 1.35 control
plane is affected by the region-level ordering above, which includes
`lf-prod-aws-ue2` — not measured here, no access from this account.

Fixing the glob to include `eks_version` is worth doing regardless of what we
decide about drivers, and is the actual fix; draining the skewed nodes only
helps until the next scale-up. It is not done in this PR because it changes
`generate_nodepools.py`, which this docs-only change deliberately leaves alone.

## Verifying current state

```bash
CLUSTER=meta-prod-aws-ue2
REGION=$(uv run scripts/cluster-config.py "$CLUSTER" region)
just kubeconfig "$CLUSTER"

# AMI a GPU node is actually running
INSTANCE=$(kubectl get nodes -l node-fleet=g5 \
  -o jsonpath='{.items[0].spec.providerID}' | sed 's|.*/||')
AMI=$(aws ec2 describe-instances --region "$REGION" --instance-ids "$INSTANCE" \
  --query 'Reservations[].Instances[].ImageId' --output text)
aws ec2 describe-images --region "$REGION" --image-ids "$AMI" \
  --query 'Images[].Name' --output text
# then look that AMI name up in the amazon-eks-ami release notes for its driver

# Node-level, from the tuning DaemonSet's own output
nvidia-smi --query-gpu=name,driver_version --format=csv,noheader
```

To re-check the kubelet skew above — which minor the glob resolves to, and what
the nodes actually booted:

```bash
aws ec2 describe-images --region "$REGION" --owners amazon \
  --filters "Name=name,Values=amazon-eks-node-al2023-x86_64-nvidia-*" \
  --query 'reverse(sort_by(Images,&CreationDate))[:3].[Name,CreationDate]' --output text

kubectl get nodes -L node-fleet -o custom-columns=\
'NAME:.metadata.name,KUBELET:.status.nodeInfo.kubeletVersion,GPU:.metadata.labels.nvidia\.com/gpu'
kubectl version -o json | jq -r .serverVersion.gitVersion
```

## References

- [CUDA Toolkit 13.4 release notes](https://docs.nvidia.com/cuda/cuda-toolkit-release-notes/index.html) — branch table, R615 requirement (13.4.1 GA; supersedes the 13.4.0 developer preview this doc first cited)
- [NVIDIA Data Center Driver 615.71.09 release notes](https://docs.nvidia.com/datacenter/tesla/tesla-release-notes-615-71-09/index.html) — R615 itself, including the proprietary-kmod removal
- `docs/nvidia-driver-615-ami.md` (PR #1068) — the parked build for option 1, and what it costs to run
- [CUDA forward compatibility](https://docs.nvidia.com/deploy/cuda-compatibility/forward-compatibility.html) — `cuda-compat` hardware and driver-branch limits
- [amazon-eks-ami releases](https://github.com/awslabs/amazon-eks-ami/releases) — per-release driver versions
- `docs/h100-fabric-handles-imex-channels.md` — records 580.159.03 / CUDA 13.0 from a July 2026 verification; superseded by 580.178.04
