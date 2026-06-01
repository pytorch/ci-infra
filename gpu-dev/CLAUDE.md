# Agent notes

the first part of this doc is the devs description of the repo. Everything under the 'AGENT SECTION' is for you, the agent, to update state, tricky things, what we're working on and more.
This will help both you, the agent, but also other agents down the road that share the responsibility of this repo management to navigate the repo.

## Agent restrictions

- NEVER run `terraform apply` or any destructive terraform commands
- You can run read-only terraform commands like `terraform plan`, `terraform state show`, etc.
- You can run AWS CLI commands for read-only resource fetching and analysis
- NEVER run destructive AWS CLI commands: `aws ec2 terminate-instances`, `aws ec2 stop-instances`, `aws autoscaling set-desired-capacity` (to 0), `aws ec2 delete-*`, `aws dynamodb delete-table`, etc. On 2026-03-09 an agent accidentally terminated 10 EC2 instances including 6 pet H100 instances from another team's capacity reservations. This must never happen again.
- NEVER run `kubectl delete node`, `kubectl drain`, `kubectl cordon`, or any command that removes/disrupts running workloads
- User will handle all infrastructure deployments themselves
- Note: We use OpenTofu, so user runs `opentofu apply` or `tf apply` locally (tf is aliased to opentofu)
- we use k for kubectl and have kubens configured to namespace gpu-dev

## Development style

We like compact code, comments when needed, but only if they add value. For example, a variable called 'number_of_threads' does not need a comment that is contains number of threads.
We like tested code.

For frontend code we use yarn, yarn format, yarn tsc. yarn dev to run code, but leave it up to the dev to run that one.
For terraform, we use opentofu, don't ever run tf apply directly. You're free to run tf state/plan and other non-breaking commands though.

**Python Code Style:**

- Always put imports at the top of the file, never inside functions or methods
- Group imports in standard order: standard library, third-party, local imports
- Use absolute imports when possible

## Testing (DO THIS FOR EVERY CHANGE)

There is a real test suite now. **Every change must keep it green, and add/adjust
tests.** Two tiers:

**1. Unit + mocks — ALWAYS run, must stay green (CI runs this on every push/PR).**
Fully mocked (boto3 / k8s / SSH / subprocess), no network, ~2s.
```bash
uv pip install -e ".[test]"        # one-time: pytest, moto, kubernetes
uv run pytest -m "not integration" # ~1140 tests; run before every commit
```
- Layout: `tests/unit/{sdk,cli,lambda_fn}/test_*.py`; shared fixtures in the root
  `conftest.py` (`cli_runner`, `lambda_index` = the lambda imported as `index`
  with env pre-set, `aws_mocks` = MagicMock boto3 handles).
- When you touch CLI / SDK / lambda code, update or add the matching `test_*.py`.
- CI: `.github/workflows/tests.yml`. Lambda imports need env vars + sys.path — the
  root `conftest.py` already sets both.

**2. e2e integration on STAGING — run for anything touching the
reserve/pod/SSH/lambda path before merging.** Real reservations on the **staging**
cluster (us-west-1), cpu + t4 only, auto-cancelled. Staging is the DEFAULT target
and github_user comes from your config, so the bare command is enough:
```bash
uv run pytest -m integration --run-integration -v
```
- Staging is the default (`GPU_DEV_TEST_ENV` defaults to `staging` → us-west-1,
  standard `pytorch-gpu-dev-*` prefix, tf workspace `default`). The integration
  conftest pins the region so the unit-test us-east-2 default can't leak in. Wired
  in `cli-tools/.../config.py` ENVIRONMENTS.
- Covers: cpu-x86 + t4 reserve→active→cancel, list-while-active, exec
  (`nproc`/`nvidia-smi`/`torch.cuda`), **`claude -p` answers "Paris"** (pod Claude
  Code/Bedrock), and the **warm pool** (fast warm claim + custom-image
  warm-ineligibility). Each cancels in a `finally` (no leaked pods).
- Warm-pool tests need `WARM_POOL_TARGETS` deployed on staging — set in
  `lambda.tf` for the `default` workspace (`{t4, cpu-x86, cpu-arm}`). Staging IS the
  tf `default` workspace (us-west-1, environment=test) — there is no `test`/`staging`
  workspace: `tofu workspace select default && tofu apply`. Until then the warm
  tests skip ("came up cold"). Custom-image test: set `GPU_DEV_TEST_IMAGE`.
- Repro test (`test_repro_known_failure.py`): set `GPU_DEV_REPRO_REF` +
  `GPU_DEV_REPRO_TEST` to a known-red (commit, test). Find one with the
  **treehugger MCP** (`hud`, user-scope — `get_hud_data`/`master_commit_red`).
  Note: prebuilt torch is h100/b200 arch, so a CUDA test on t4 needs a full build;
  prefer a failure that runs on the box's GPU or on cpu.
- Skips cleanly if staging is unreachable or the runner has no outbound SSH (e.g. a
  sandbox). The reservation role can query/SQS but lacks `DescribeTable`, so the
  reachability probe uses scan+get-queue-url, not describe.
- Validated live (2026-05-31): cpu + t4 lifecycle PASS; warm-claim test confirmed
  it reaches the real reserve (skips until WARM_POOL_TARGETS is applied).

**Rule of thumb:** unit+mocks for *every* change; add e2e coverage when you add a
new command/flow; run the staging e2e before merging anything that could affect a
live reservation. Don't say "done/tested" without having run the relevant tier.

## Content

- torchci - a next.js app containing a PyTorch CI tracker
- aws - a bunch of lambdas & amis that are used in the tf module
- terraform-aws-github-runner - the definition of repos tofu modules. These modules are used in another repo to be deployed.
- cli-tools - the home of the gpu-dev cli tool that is used for creating/listing/cancelling reservations

## Current challenge and WIP

Currently we're working on a developer servers with GPUs in AWS. This means we'll need:

- a CLI tool for devs to reserve a server [DONE]
- a queue of open requests [DONE]
- a reservation for 2 EC2 H100 servers
- a way for devs to specify if they want 1/2/4/8 GPUs of a server [DONE]
- later, a way for devs to specify 2x8 GPUs, so they want a connected 2 server setup reserved for X hours
- we care about NIC connection - NVLINK or as fast as possible in one region / subregion.
- a lambda to process items from the queue if servers are available [DONE]
- a managed k8s to reserve, start a pod, interactive, and reserve that one for X hours for the dev (configurable) [DONE]
- auth can be through github public keys, all devs already have those exposed. This should be for devs with commit access to pytorch/pytorch only though. And part of metamates group in Github. [DONE]

# AGENT SECTION

## Instant-sandboxes branch — WIP & things to fix (2026-05-29)

Big push on warm pools + instant claims + prebuilt pytorch. Tracking state here so it's not lost.

**Committed, needs deploy/activation:**
- `tf apply` (branch `instant-sandboxes`): warm-pool reconciler + fail-open claim hook, async hot-refill on claim, async per-user EFS mount, processor self-invoke IAM, Bedrock marketplace perms on pod IRSA, pytorch `ref` staging, availability counts warm-ready as available, git-cache worktree snapshot + `pytorch-snapshot` DaemonSet, processor Function URL.
- Reinstall **CLI + SDK**: `--direct` (default on) synchronous claim, `--ref` (pr/commit/branch), `--no-persist`+`--disk` conflict guard, Function-URL cache (`~/.config/gpu-dev/direct-url.json`).
- Rebuild **gpu-dev image**: Claude Code cache-bust (latest), `~/.local/bin` on PATH (bash+zsh, all disks).
- **Meta/fbcode**: grant the user IAM role `lambda:InvokeFunctionUrl` + `lambda:GetFunctionUrlConfig` (scoped to reservation-processor) so `--direct` works; otherwise it falls back to SQS silently.

**Prebuilt viable/strict + warm ccache (importable torch + marginal C++ build) — COMMITTED on `instant-sandboxes`, needs `tf apply`:**
- [x] Dedicated `m7i.48xlarge` build node group (always-on). `build-node.tf`, node `ip-10-0-26-237` up.
- [x] Hourly **stateful incremental** build CronJob (`pytorch-prebuild.tf`): `concurrencyPolicy=Forbid` + flock (the "build queue"), **CUDA 13.2** (matches the cu13 nvshmem ABI in the image — 12.8 fails at nvlink), `TORCH_CUDA_ARCH_LIST=9.0;10.0` (see arch note below), `BUILD_TEST=0`, builds at **`/home/dev/pytorch`** on a hostPath (path-match for relocatable incremental), `CCACHE_DIR=/ccache_shared/build-node`, only when viable/strict SHA bumps. Publishes via rsync to `/ccache_shared/prebuilt/pytorch-<arch>`.
- [x] `pytorch-snapshot` DaemonSet (in `git-cache.tf`) arch-aware: rsyncs the built tree from the shared EFS to each node's `/mnt/nvme/pytorch-built` (arch via `uname -m`; arm skips gracefully). Existing master worktree HTTP pull unchanged.
- [x] `stage-pytorch` (lambda) reflink-copies the built tree into `/home/dev/pytorch` + sets `PYTHONPATH` (`/etc/profile.d/zz-pytorch.sh` + `*_ext`) so `import torch` works with no pod-side build. With `--ref`: same tree (warm `build/`), checkout the ref, rebuild is incremental. Applies to warm pods too.
- **Publish/cache decision:** reuse the existing `ccache_shared` EFS (everyone already mounts it) under `/prebuilt`; no new EFS/S3. EFS here = plain NFS volume mounts, not CSI. ccache is shared by build node + ALL dev pods (incl persistent-disk) so a user's own build benefits from the build node's compiles.
- **Validated build numbers** (m7i, 128 jobs, CUDA 13.2, `9.0;10.0`, BUILD_TEST=0): cold (build/ gone, ccache 86% warm = node-replacement case) **~21m**; incremental (1 cutlass kernel + 386MB relink) **~42s**; ninja no-op **~22s**; ccache **86.5%** hit. Result: `torch 2.13.0a0`, imports, `get_arch_list()=['sm_90','sm_100']`.
- [ ] **Cleanup:** delete the manual test pod `gpu-dev-buildtest` (gpu-dev ns) — done with empirical measurement (kept for now in case more measurements needed). It holds a warm `/root/pt` build tree.
- [ ] **Reflink caveat:** stage-pytorch uses `cp -a --reflink=auto || cp -a`. For the drop-in to be *instant* (not a 20-40GB copy), the pod's `/home/dev` (dev-home emptyDir) and the node's `/mnt/nvme` must be the **same filesystem**. Verify node bootstrap puts kubelet emptyDir on `/mnt/nvme`; else it falls back to a full copy (correct, slower).

**To fix / todo:**
- [ ] **Direct/warm claim path drops `--ref` and `--no-persist`:** a `reserve --ref X --no-persist` (no `--disk`) still satisfies the line-1388 `claim_direct` condition (it doesn't exclude `ref`), so it goes the warm/direct path which doesn't carry `ref`/`no_persistent_disk` → the user got their **default persistent disk** + no PR staged (reservation `5e83bb5b`: `no_persistent_disk=false, disk_name=default, pytorch_ref=null, version=null`). Fix: exclude `ref` (and honor `no_persistent_disk`) from the direct fast-path, OR thread `ref`/`no_persistent_disk` through `claim_direct`+`handle_direct_claim`. Workaround for now: `--no-direct --no-persist --ref`.
- [x] **Warm full-GPU (1-GPU) pods + evict-on-demand** (DONE, commit c1211e3): `_evict_warm_for_capacity` deletes the minimum warm-ready pods on a single node when no node has enough free GPUs (gated in `get_target_az_for_reservation` before the Pending fallback; reconciler tops the pool back up). Also covers full **MIG** nodes filling up (not just full-GPU) — warm pods no longer block 2/4/8-GPU or full-node requests. Added `WARM_POOL_TARGETS` `h100:1, b200:1` (safe now that they're evictable). `get_available_gpus_on_node` counts warm pods as used, so placement avoids them until eviction frees them. Needs `tf apply`.
- [ ] **CLI install hygiene:** user's `~/.venv` has BOTH `gpu-dev 0.6.6` (editable→repo) and a stale duplicate `gpu-dev-cli 0.3.5` (also editable, same dir, different dist name). `pip uninstall gpu-dev-cli` to remove the confusing duplicate; the real package is `gpu-dev`.
- [ ] **Publish via tarball, not rsync-to-EFS:** rsync of the raw tree (.git + build/ = 100k+ small files) to EFS stalled at 0 files in 13min (NFS per-file round-trips). Switched publish + DaemonSet to a single `zstd` tarball (sequential I/O). (committed)
- [ ] **Prebuilt built WITHOUT cuDNN** — `import torch` warns "compiled without cuDNN/MIOpen". CI/nightly build with cudnn9. Add libcudnn to the gpu-dev image + `USE_CUDNN=1` to the build recipe for fidelity (conv/cudnn-dependent ops + tests). Irrelevant for flex-attention int64 test; matters generally.
- [ ] **`--ref pr/N` uses `pull/N/head`, not `/merge`** — `/head` is the PR author's raw branch tip (often based on old trunk, missing trunk-added tests); CI tests `/merge` (PR merged onto current trunk). For CI-repro fidelity, `pr/N` should fetch `pull/N/merge` (fall back to `/head` if no merge ref). `stage-pytorch` REF case in `index.py`. (This is why `pull/185479/head` lacked `test_large_kv_int64_pointer_math_cuda`.)
- [ ] **Misleading disconnect/expiry message** — on `gpu-dev connect` connection loss OR reservation expiry, the CLI prints "❌ Authentication failed. You don't have SSH access... ask the primary user to add you" even for the PRIMARY user's own expired/cancelled reservation. Distinguish: (a) reservation expired -> "Reservation <id> expired at <time>"; (b) cancelled -> "Reservation was cancelled"; (c) connection dropped but still active -> "Connection lost, reconnect with gpu-dev connect <id>"; (d) genuine auth failure -> the current add-user message. Check reservation status before assuming auth failure.
- [x] **`gpu-dev cancel` from inside the pod** (DONE, 0.7.5) — two bugs: (1) cancel inside a **warm-claimed** pod failed with "GitHub username not configured" because the warm pod was pre-booted with `user_id="warm"` and the claim never stamped the real identity → `GPU_DEV_USER_ID/GPU_DEV_GITHUB_USER/AWS_ROLE_SESSION_NAME` stayed `"warm"`/empty. Fix: `try_claim_warm_pod` now seds the real `user_id`/`github_user` into both `.bashrc_ext`/`.zshrc_ext` + writes `GPU_DEV_RESERVATION_ID` (full id). Cold `_ext` derives `GPU_DEV_RESERVATION_ID` from the hostname (8-char prefix; cancellation resolves by prefix). (2) `gpu-dev cancel` (no id) inside a pod now fast-paths: cancels THIS reservation directly via `GPU_DEV_RESERVATION_ID`+`GPU_DEV_USER_ID` (no github_user/interactive) with the graceful "🛑 Shutting down..." message. Needs `tf apply` (lambda) + image rebuild (CLI in pods).
- [ ] SSH CA certs to drop the ~0.33s `kubectl exec` key injection on warm claim (auth-model change).
- [ ] AMI baker re-bakes on every base-EKS-AMI roll (5 baked AMIs in 2 days): pin the base AMI version + clean up old `gpu-dev-baked-*`.
- [ ] **Warm pods: gate `warm-state=ready` on staging completion** (NOW MORE IMPORTANT — the built tree is ~30GB, and on GPU nodes it's a `cp` not reflink, so staging takes ~1-3min; a claim in that window hands over a half-copied tree). Two options: (a) claim-time check — exec `[ -f /home/dev/.pytorch-staging ]` in `try_claim_warm_pod`, skip pods still staging (simple, but adds ~0.5s exec to every warm claim); (b) label-flip — create with `warm-state=provisioning`, reconciler exec-checks staging + flips to `ready` (no claim latency, but 4 interacting changes: create label + reconciler flip + eviction must also target `provisioning` + claim already filters `ready`). Prefer (b). Marker: `.pytorch-staging` present during, removed when done; `.pytorch-ready` written at end.
- [ ] **Image-rebuild propagation gap:** pods use `imagePullPolicy=IfNotPresent` + `:latest`, so a rebuilt image does NOT reach pods until the node re-pulls. After every image rebuild you must `kubectl rollout restart daemonset gpu-dev-image-prepuller -n kube-system` (re-pull on all GPU nodes, ~5min) **and** recycle warm pods, else pods run the stale cached image (this is why claude/PATH looked unfixed). Automate later: reconciler recycles warm pods when the `:latest` digest changes (and/or trigger the prepuller restart from the image-build step).
- [x] **Prebuilt build archs (CORRECTED):** use plain `TORCH_CUDA_ARCH_LIST=9.0;10.0` — **NOT** `9.0a;10.0a`. You never put the `a` in the list yourself. PyTorch's `cmake/Codegen.cmake` (`_BUILD_FOR_ADDITIONAL_ARCHS`, gated on `compute_90`/`compute_100` being present) auto-adds `sm_90a`/`sm_100a` to exactly the cutlass kernels that need Hopper wgmma/TMA (`RowwiseScaledMM.cu`, `ScaledGroupMM.cu`, `GroupMM.cu`). Verified in `compile_commands.json`: the RowwiseScaledMM line shows all four (sm_90, sm_90a, sm_100, sm_100a). Forcing `9.0a` for the whole build is non-CI and would drop the plain SASS / other archs. Per-commit **trunk** CI builds narrow per-runner arch (`9.0` alone for H100 jobs, `10.0` for B200) — nightly builds the fat `7.5;8.0;8.6;9.0;10.0;12.0+PTX`; we match trunk + "9+" for our H100/B200 fleet. To add A100/T4/L4 later, widen to `8.0;8.9;9.0;10.0` (still one build). CUDA 13.2 (image default), not 12.8.

## Issues I found with the description above

- I am not sure terraform-aws-github-runner is correctly described. Next time I go over this code for maintenance or adding something, I'll inform the user of what I think should change. This is not an active goal though, just a sidequest.
- The user asked for NIC connections. I still need to figure out how fast and what's avaiable @ AWS, When I do that, I'll update this section below:

## NIC explanation in AWS

**EFA (Elastic Fabric Adapter):**

- Low-latency, high-throughput networking for HPC/AI workloads
- 3200 Gbps bandwidth on p5.48xlarge instances
- RDMA support, bypasses kernel for direct hardware access
- Integrates with NVIDIA NCCL for multi-GPU communication
- **Critical limitation**: Cannot cross Availability Zones - all instances must be in same AZ

**H100 Instance Performance (p5.48xlarge):**

- 8x NVIDIA H100 GPUs (80GB each = 640GB total GPU memory)
- Within instance: GPUs use NVLINK folr direct communication
- Between instances: EFA provides fastest networking option
- Single AZ placement group recommended for best performance

**K8s Decision:** EKS with GPU-optimized EC2 node groups (Fargate has no GPU support)

## Multi-Node NCCL Communication (Mar 2026)

**Working Configuration (SENDRECV protocol):**
- Protocol: `OFI_NCCL_PROTOCOL=SENDRECV` (host-staged EFA, avoids RDMA mr_regattr failures)
- GDR disabled: `FI_EFA_USE_DEVICE_RDMA=0`, `NCCL_NET_GDR_LEVEL=0`
- Socket interface: `NCCL_SOCKET_IFNAME=^lo,docker` (H100 nodes use enp71s0/enp72s0, NOT eth0)
- Algorithm: `NCCL_ALGO=ring,tree` (NCCL auto-selects tree for large messages, ~2x faster)
- Exclude Mellanox: `NCCL_IB_HCA=^mlx`
- OpenMPI lib path: `/opt/amazon/openmpi/lib` (NOT lib64 — EFA installer puts it in lib)

**Benchmark Results (2x p5.48xlarge, 16 GPUs):**
- Ring algorithm: ~9.5 GB/s avg bus bandwidth, ~13.4 GB/s peak
- Tree algorithm: ~21.4 GB/s avg bus bandwidth, ~33.6 GB/s peak
- Ring+tree combined: ~21.0 GB/s avg (NCCL auto-selects tree for large msgs)
- Single-node NVLink: ~34 GB/s (for reference)

**GDR Status (NOT working — future optimization):**
- EFA RDMA protocol fails: `fi_mr_regattr` returns EFAULT for flush buffer (even host memory)
- EFA device version: 6 (above aws-ofi-nccl blocklist threshold of 1-3)
- EFA kernel driver: 2.17.2a (need 2.17.3+ which has "Support P2P with NVIDIA 580 drivers")
- nvidia-peermem: NOT available (module not found for kernel 6.12.68)
- efa-nv-peermem: NOT installed (available in amzn-drivers repo, works with open NVIDIA drivers)
- To enable GDR in future: install efa-nv-peermem module on host nodes, or update EFA kernel driver
- Expected GDR improvement: ~300-370 GB/s bus bandwidth (vs ~33 GB/s current)

## Implementation Status (Jan 11, 2025)

### ✅ Completed and Working

- **Infrastructure**: Dual-mode EKS with managed vs self-managed node groups for faster development
- **Networking**: Full DNS resolution and internet access for pods (CoreDNS + security groups fixed)
- **SSH Access**: Complete SSH server setup with proper package installation and daemon startup
- **Authentication**: GitHub public key fetching (ALL user keys, not just first one)
- **CLI Features**: Float hours support (e.g., --hours 0.25 for 15 minutes)
- **Reservation Display**: CLI list command shows formatted expiration times (YYYY-MM-DD HH:MM:SS)
- **Security Groups**: Full connectivity - kubelet (10250), control plane (443), DNS (53), NodePort (30000-32767)
- **Python CLI tool**: Commands: reserve, list, config with real-time polling
- **SQS + Lambda**: Async queue processing system with DynamoDB state tracking
- **Kubernetes**: Pod creation with GPU allocation, NodePort services, init containers
- **Expiry System**: Timestamp-based expiration tracking with historical records (TTL disabled)
- **DynamoDB**: Reservations kept as historical records, not auto-deleted
- **SSORole + instructions for that** - Implement SSO role authentication and provide setup instructions
- **Rename G6 to L4** - Update G6 references to L4 (similar to T4 GPU type naming)
- **Add network drive (EFS)** - Implement 20TB EFS shared storage mounted at /shared with user folders
- **GPU Profiling Support** - Added NVIDIA profiling capabilities for all pods:
  - Node-level: Added `options nvidia NVreg_RestrictProfilingToAdminUsers=0` to `/etc/modprobe.d/nvprof.conf` in node bootstrap script - automatically configured on ALL new GPU nodes
  - Bootstrap: Configuration added at `terraform-gpu-devservers/templates/al2023-user-data.sh:17-19` (applied BEFORE NVIDIA driver installation to avoid auto-load issue)
  - Pod-level: Added Linux capability `SYS_ADMIN` to all GPU pods (required for NVIDIA profiling tools like ncu/nsys)
  - Environment: Set `NVIDIA_DRIVER_CAPABILITIES=compute,utility` (note: `profile` is NOT supported by NVIDIA device plugin)
  - Location: `terraform-gpu-devservers/lambda/reservation_processor/index.py:4000` and `:3984`
- **GPU Monitoring with Grafana** - Added full GPU monitoring stack:
  - DCGM Exporter enabled in GPU Operator with anti-affinity for profiling nodes
  - kube-prometheus-stack deployed with 50GB persistent storage (15-day retention)
  - Grafana accessible via NodePort 30080 on any node IP
  - Pre-loaded NVIDIA DCGM dashboard (Grafana ID 12239) + custom GPU Overview dashboard
  - Configuration: `terraform-gpu-devservers/monitoring.tf`

## GPU Monitoring & Profiling Node Setup (Dec 2025)

**Architecture:**
- DCGM Exporter runs on ALL GPU nodes EXCEPT profiling-dedicated nodes
- Profiling-dedicated nodes: ONE H100 and ONE B200 node reserved for Nsight profiling
- DCGM and Nsight conflict because both need exclusive GPU access

**Profiling Node Labeling (manual, one-time setup after `tf apply`):**
```bash
# List H100 nodes and pick ONE for profiling
kubectl get nodes -l gpu-type=h100

# Label one H100 node as profiling-dedicated (DCGM will NOT run on this node)
kubectl label node <h100-node-name> gpu.monitoring/profiling-dedicated=true

# List B200 nodes and pick ONE for profiling
kubectl get nodes -l gpu-type=b200

# Label one B200 node as profiling-dedicated
kubectl label node <b200-node-name> gpu.monitoring/profiling-dedicated=true

# Verify labels
kubectl get nodes -l gpu.monitoring/profiling-dedicated=true
```

**Grafana Access:**
```bash
# Get any node IP
kubectl get nodes -o wide

# Access Grafana at: http://<node-ip>:30080
# Default credentials: admin / (value of grafana_admin_password variable)
```

**Available Dashboards:**
- NVIDIA DCGM Exporter Dashboard (pre-configured from Grafana community)
- GPU Overview (custom dashboard with utilization, memory, temp, power)

**Troubleshooting:**
```bash
# Check DCGM pods are running (should NOT be on profiling nodes)
kubectl get pods -n gpu-operator -l app=nvidia-dcgm-exporter -o wide

# Verify Prometheus is scraping DCGM
kubectl port-forward -n monitoring svc/kube-prometheus-stack-prometheus 9090:9090
# Then open http://localhost:9090 and query: DCGM_FI_DEV_GPU_UTIL

# Check Grafana pods
kubectl get pods -n monitoring -l app.kubernetes.io/name=grafana
```

## Multi-Region Single-State Refactor (Research Notes, May 2026)

**Goal:** One `tf apply` manages all regions. No more `tf-all`, no double Docker builds, no double AMI bakes.

**Approach:** Module-per-region pattern.
```hcl
# root main.tf
module "us_east_2" {
  source    = "./modules/region"
  region    = "us-east-2"
  gpu_types = { h100 = {...}, b200 = {...}, ... }
  spot_types = []
  providers = { aws = aws.us_east_2 }
}
module "us_east_1" {
  source    = "./modules/region"
  region    = "us-east-1"
  gpu_types = { b300 = {...}, t4 = {...}, ... }
  spot_types = ["b300", "b200", "h100", ...]
  providers = { aws = aws.us_east_1 }
}
```

**What goes in the module:** VPC, subnets, EKS cluster, ASGs, launch templates, Lambda functions, DDB tables, EFS, monitoring, DNS. Basically everything in the current root except provider config and shared resources.

**What stays at root:** Provider blocks with aliases, ECR replication config, AMI copy (`aws_ami_copy` from primary to secondary regions), global IAM roles if any, CLI config.

**AMI sharing:** Build baked AMI in us-east-2 (primary), `aws_ami_copy` to other regions. One build, replicated. The `ami_baker` stays in root, outputs AMI ID, each module receives it as a variable.

**Docker sharing:** ECR replication already set up. Docker builds once in primary region, auto-replicates.

**Migration plan (since nobody uses east1 yet):**
1. `tofu workspace select prod-east1 && tofu destroy` — clean slate
2. Move all resources into `modules/region/`
3. Create provider aliases in root
4. Import prod (us-east-2) resources into new module state: `tofu import module.us_east_2.aws_vpc.gpu_dev_vpc vpc-xxx`
5. Add us-east-1 module — fresh create, no import needed
6. Delete workspace: `tofu workspace delete prod-east1`

**Risks:**
- Import step for prod is tedious (~50+ resources) but mechanical
- Lambda zip paths need to be relative to module, not root
- EKS auth (aws-auth ConfigMap) is per-cluster — each module manages its own
- CLI needs to know which region to query — already handled by config

**Estimated effort:** 1 dedicated session (~4-6 hours). Most time on the module extraction + prod import.

**Prerequisite for:** Adding us-west-1, us-west-2, or any future region (becomes one module block each).

## Recent Fixes (Oct 27, 2025)

**NVIDIA Profiling Bootstrap Configuration (Oct 27, 2025):**
- **Bug Found**: NVIDIA driver installation (`dnf install nvidia-driver`) automatically loads kernel modules during install, so config must be created BEFORE driver installation, not just before explicit modprobe
- **Fix**: Moved `echo "options nvidia NVreg_RestrictProfilingToAdminUsers=0" > /etc/modprobe.d/nvprof.conf` to line 19 (before driver install at line 23)
- **Previous Location**: Line 59-60 (after driver install) - TOO LATE, modules already loaded during dnf install
- **New Location**: `terraform-gpu-devservers/templates/al2023-user-data.sh:17-19` (before driver installation)
- **Benefit**: All new GPU nodes will have profiling enabled automatically without requiring manual configuration or reboots
- **Rollout**: Run `tf apply` to update launch template, then terminate existing nodes so ASG recreates them with new bootstrap script

## Recent Fixes (Oct 8, 2025)

**Kubelet Auto-Start Issue on T4 Nodes:**
- **Problem**: After rebooting T4 nodes to apply NVIDIA profiling config, kubelet didn't auto-start
- **Root Cause**: `systemctl enable kubelet` wasn't being called during node bootstrap
- **Temporary Fix**: Manually enabled and started kubelet on all 5 T4 nodes via SSH
- **Future**: Nodes should be terminated and recreated by ASG to get fresh bootstrap (user-data runs nodeadm which should enable kubelet)

**Decimal/Float Type Error in Lambda:**
- **Problem**: `unsupported operand type(s) for *: 'decimal.Decimal' and 'float'` error when allocating GPU resources
- **Root Cause**: DynamoDB returns numbers as `Decimal` type, but Lambda code was multiplying with Python floats
- **Fix**: Added `gpu_count = int(gpu_count)` at start of `get_pod_resource_limits()` and `get_pod_resource_requests()` functions
- **Location**: `terraform-gpu-devservers/lambda/reservation_processor/index.py:3034` and `:3117`

**NVIDIA Profiling Configuration:**
- **Problem 1**: Pods failed with "unsupported capabilities found in 'compute,profile,utility' (allowed 'compute,utility')"
  - Fix: Removed `profile` from `NVIDIA_DRIVER_CAPABILITIES`, kept only `compute,utility`
- **Problem 2**: Profiling failed with "driver resource unavailable" even with `CAP_PERFMON` and `CAP_SYS_PTRACE`
  - Fix: Changed to `CAP_SYS_ADMIN` which is required for NVIDIA GPU profiling (ncu, nsys)
- **Root Cause**: NVIDIA profiling tools need full SYS_ADMIN capability to access driver resources
- **Final Config**: `SYS_ADMIN` capability + node-level `NVreg_RestrictProfilingToAdminUsers=0`
- **Location**: `terraform-gpu-devservers/lambda/reservation_processor/index.py:4000` and `:3984`

**No Persistent Disk Flag (Oct 8, 2025):**
- **Problem**: When user created 2nd reservation and confirmed "continue without persistent disk", Lambda waited 60s for disk detachment, timed out, set status to "failed", but then CONTINUED execution and restored from snapshot anyway
- **Root Cause 1**: The timeout logic at line 305 raised `RuntimeError` which was caught by outer try-except block at line 2108, but `persistent_volume_id` variable remained set from earlier operations, so pod creation still used a persistent disk
- **Root Cause 2**: Exception handler at line 2275 only set `use_persistent_disk = False` but didn't clear `persistent_volume_id`, so any disk created/restored before the exception would still be attached to the pod
- **Fix Part 1 - Explicit Flag**: Added `no_persistent_disk` flag that flows from CLI through SQS to Lambda
  - CLI: When user confirms to continue without persistent disk, sets `no_persistent_disk=True` in SQS message
  - Lambda: Checks `no_persistent_disk` flag early (line 2087-2090) and skips ALL persistent disk logic if true
  - Files: `cli-tools/gpu-dev-cli/gpu_dev_cli/cli.py:914`, `reservations.py:396,450,487,544`, `lambda/reservation_processor/index.py:2087-2090`
- **Fix Part 2 - Exception Cleanup**: Updated exception handler at line 2275 to properly clean up state
  - Sets `persistent_volume_id = None` to clear any volume created before the error
  - Sets `is_new_disk = True` so EmptyDir gets proper shell environment setup
  - Location: `lambda/reservation_processor/index.py:2279-2280`
- **Benefit**: No more waiting for disk detachment, no snapshot restoration, clean EmptyDir volume from the start. Even if disk operations fail mid-way, exception handler ensures no disk is attached.

### 📋 Remaining Tasks

- **Merge multi-region into single tf state** - HIGH PRIORITY. Kill prod-east1 workspace, refactor into module-per-region in one state. See research notes below. Enables: one `tf apply`, shared AMI (aws_ami_copy), shared Docker (ECR replication already set up), no double builds. Prerequisite for adding west regions.
- **Add us-west-1 and us-west-2 spot regions** - BLOCKED on single-state refactor. After refactor, adding a region = adding one module block.
- **Spot UX improvements** - Queue position should be #1 for each type (not cross-type FIFO). Status should show "queued (waiting for capacity)" not just "queued". Interactive picker should show spot GPU counts from east1 not prod. NOTE (2026-05-30): spot is now **hidden by default** in `gpu-dev reserve` (interactive picker), `gpu-dev avail`, and watch mode — `cpu-spot` + the us-east-1 spot cluster only appear with `--spot` (reserve/avail flag) or the "⚡ Show spot options" picker entry. Spot was too bloated/half-baked for the default view. CLI-only change (`cli.py` `_show_availability`/`_show_availability_watch`/`avail`/`reserve`, `interactive.py` `select_gpu_type_interactive`).
- **FQDN for devservers** - Set up proper domain names for development server access
- **Automated SSH config per reservation** - ✅ DONE - Each reservation now gets `~/.devgpu/<reservation_id>-sshconfig` file, use with `ssh -F ~/.devgpu/<reservation_id>-sshconfig <pod_name>`
- **Custom Docker image scaffold** - Create Dockerfile with pre-installed packages (Jupyter, etc.)
- **Add Docker CI image run** - allow user to specify gpu-dev ci-debug <testurl> that downloads that docker-image and goes for it
- **Increase /dev/shm for NCCL** - Bump /dev/shm space from 64MB for NCCL requirements (https://docs.nvidia.com/deeplearning/nccl/user-guide/docs/troubleshooting.html#docker)
- **Add nvcuvid.so support** - Enable NCU (NVIDIA Nsight Compute) support with nvcuvid.so library

- **Make gpu-type case agnostic** - Allow case-insensitive GPU type parameters (e.g., h100, H100, HuNdred should all work)
- **Error on non-existing GPU type** - Error out if people ask for a non-existing GPU type
- **Error on too many GPUs** - Error out if people ask for more GPUs than available in node (8 for H100/B200, 4 for T4, etc.)
- **Fix GPU SKU validation** - Add proper error handling for non-existing/unavailable GPU types (e.g., user requesting A100 when only T4 available should get immediate error, not pending pod that will never schedule)
- **Set HuggingFace cache location** - Set HF_HOME or XDG_CACHE_HOME to /tmp or /workspace so HuggingFace doesn't fill up user home directories with model downloads
- **Add verbose CLI output** - More detailed status and progress information for debugging
- **Interactive CLI for cancel/edit** - Make `gpu-dev cancel` and `gpu-dev edit` interactive when no reservation ID specified - show list with up/down arrow selection
- **Default reservation edit/cancel** - Auto-select reservation if user only has one active
- **Add a command gpu-dev availability** that shows how many gpus of each type are available to reserve at the moment, and if 0, what the estimated queue time is
- **Production deployment** - Switch to p5.48xlarge instances when ready
- **Investigate NFS** - Research NFS integration for shared storage across pods
- **Persistent disk** - Implement persistent disk storage for user data across sessions
- **Validate CUDA version** - Add CUDA version validation and display in container startup
- **Validate NVIDIA driver version** - Display and validate NVIDIA driver version
- **Test wall messages** - Verify that wall message functionality works correctly
- **Validate if expiration works as expected** - Test and verify pod cleanup and reservation expiry process
- **Simplify code + clean up** - Refactor and clean up codebase for maintainability
- **Add Docker** - Install and configure Docker in development containers - maybe --docker at reserve, which will use dind if possible to the container (to investigate how feasible)
- **Add ghstack** - Install ghstack tool for GitHub stack management
- **Improve debugging and observability** - Add better CLI feedback for pod status, container logs, and error details. Current debugging experience is poor - users need kubectl/aws cli knowledge to debug issues. CLI should show:
  - Real-time pod startup logs during `gpu-dev reserve`
  - Container error messages when pods fail
  - Image pull status and errors
  - Resource allocation details
  - More detailed error messages with troubleshooting hints
- **Add CloudWatch logs for pods** - Store pod logs in CloudWatch for better debugging and monitoring
- **Add tests for everything** - Implement comprehensive test suite for all components
- **Investigate multi node communication** - Research inter-node networking for multi-GPU setups
- **Switch between H100/B200 GPU types** - Add `--gpu-type=b200` CLI option with separate queues per GPU type
- **GPU queue status command** - Add status command to show queue length per GPU type (eg, `gpu-dev queue-status`)
- **Jupyter notebook integration** - Add `--jupyter` flag to enable Jupyter notebook and TensorBoard access
- **Add user collaboration feature** - Add `--add-user <github_name>` flag to allow users to add someone to the server
- **Display Bug:** - CLI shows "G6" instead of "L4" in availability table - likely resolves on prod release when Lambda functions are updated with new GPU type mappings
- **Fix extend command warning cleanup** - When using `--extend`, the system doesn't remove the WARN_EXPIRES_IN_5MIN.txt file and doesn't reset the expiry warning tracking in the database. Need to either clear the warning state from the table or keep warning history elsewhere for auditing purposes
- **Max reservation time: 48 hours** - Maximum reservation duration is 48 hours (initial 24h + one 24h extension allowed)
- **Scale up T4 instances** - Add 3 more T4 nodes (g4dn.12xlarge) to cluster
- **Scale up L4 instances** - Add 3 more L4 nodes (g6.12xlarge) to cluster
- **Add on-demand H100/H200/B200 capacity** - Add at least 2 nodes each of H100 (p5.48xlarge), H200 (p5e.48xlarge), and B200 (p6-b200.48xlarge) as on-demand capacity in addition to existing reserved instances
- **Run pytorch tests via gpu-dev** - Add a way to run a specific test / set of tests in ../pytorch (see `python run.py` in pytorch for how tests are normally invoked). Short term: `gpu-dev test <paths/test ids>` that reserves, stages pytorch (via --ref), and runs the test command. Long term (stretch, "magic TD"): an agent does target determination from the repo diff, picks the affected tests, kicks off a gpu-dev run, and streams test output back. Builds on the warm-pool + pytorch-snapshot work (instant-sandboxes branch).
- **Warm pool follow-ups** (from instant-sandboxes branch):
  - Claim-with-ref: today an explicit `--ref` skips the warm pool (cold path). Could instead claim a warm pod and incrementally `git fetch`+checkout the ref in-place.
  - Availability display: warm-ready pods count as "used" in the availability table, so `gpu-dev avail` under-reports free MIG/CPU even though a claim is instant. Reconcile the display with warm claimability.
  - CPU/MIG node disk: the pytorch-snapshot DaemonSet writes ~5-10GB to /mnt/nvme (root disk on nodes without instance NVMe); confirm CPU dev node root volumes are sized for it.
- **Future features**:
  - Multi-server (16 GPU) reservations
  - GitHub organization/team verification
  - Reservation extensions
  - Usage monitoring and quotas

## Current Working Architecture

**Infrastructure (us-east-2):**

- **Current**: 2x p4d.24xlarge instances (8 A100 GPUs each = 16 total GPUs)
- **Previous testing**: 2x g4dn.12xlarge instances (4 T4 GPUs each = 8 total GPUs)
- **Future**: 2x p5.48xlarge instances (8 H100 GPUs each = 16 total GPUs) when capacity available
- EKS cluster with GPU-optimized node groups
- NVIDIA device plugin for GPU resource exposure
- Single AZ deployment with cluster placement groups

**Reservation System:**

- SQS queue for async reservation requests
- Lambda functions for pod creation and expiry management
- DynamoDB for reservation and server state tracking
- Kubernetes pods with GPU resource allocation (1/2/4 GPUs)
- NodePort services for SSH access to pods

**Authentication & Access:**

- GitHub username configuration for SSH key fetching
- Public key injection into pods via init containers
- Copy-pasteable SSH commands with NodePort access

**CLI Tool:**

- Python CLI with config at `~/.config/gpu-dev/config.json`
- Commands: `reserve`, `list`, `config`
- Real-time polling until reservation is ready
