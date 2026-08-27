# ODC Feature Release - May 2026

Three new ways to use your dev cluster: one-shot jobs, jump pods, and a brand-new spot region.

## 🚀 What's New

### 1. **`gpu-dev submit` — sbatch-style one-shot jobs**

Reserve, sync your code up, run, sync results back, and auto-cancel — all in one command. Think `sbatch` but for our cluster.

```bash
# Run train.py on 16 H100s, get checkpoints back locally
gpu-dev submit \
  --gpu-type h100 --gpus 16 --runtime ./ \
  -- bash run.sh
```

**What it does (in order):**
1. Reserves the requested GPUs (waits up to `--timeout` minutes; default 24h)
2. `rsync ./` up to `/workspace/submit-<id>/` on rank 0
3. Runs your command in a login shell (so `MULTINODE_HOSTS`, `MASTER_ADDR`, etc. are loaded)
4. `rsync` the workspace back to your local dir — checkpoints, logs, everything
5. Cancels the reservation

Exit code mirrors your remote command. `Ctrl+C` cancels mid-run. `--keep-alive` skips auto-cancel for debugging. `--no-pull` skips the sync-back.

**Real example — Bob Ren's daily NCCL benchmark on 16 H100s:**

```bash
gpu-dev submit --gpu-type h100 --gpus 16 --runtime ./ -- bash run.sh
```

Where `run.sh` is just:
```bash
mpirun --host $(...IPs from $MULTINODE_HOSTS...) -np 16 \
  --mca plm_rsh_args "-p 2222" \
  -x NCCL_DEBUG -x NCCL_ALGO -x FI_PROVIDER \
  /opt/nccl-tests/build/all_reduce_perf -b 1M -e 1G -f 2 -g 1 -n 20
```

Runs from rank 0, orchestrates the whole cluster via mpirun + the passwordless SSH between pods we set up automatically. Results in `nccl-all_reduce.log` locally when it finishes.

**EFA reminder:** on `p5/p5e/p5en/p6-b200` instances you get **full RDMA via EFA — 3200 Gbps (~400 GB/s) per node**, ~30-40× faster than TCP. Submit picks up these settings automatically when you use 8+ GPUs of the same type.

**Smoke tests** to verify your setup: `tests/submit/{success,fail,multinode}/`.

### 2. **Jump pods — run `gpu-dev` from inside `gpu-dev`**

CPU pods can now be held for days/weeks (like your CPU devvm) and used as a "head node" for the cluster.

```bash
# Reserve a long-lived CPU pod with persistent disk
gpu-dev reserve --gpu-type cpu-x86 --hours 720 --disk default

# SSH in
gpu-dev connect <id>

# Now from inside the pod — gpu-dev is pre-installed AND authenticated
gpu-dev list                                          # works
gpu-dev submit --gpu-type h100 --gpus 8 -- bash run.sh  # works
```

**What changed under the hood:**
- `gpu-dev` is now bundled in the base image — no `pip install` step
- Auto-upgrades in the background on pod startup so you always get the latest
- AWS auth via IRSA — your IAM identity is passed through automatically, no `aws sso login` needed
- The CPU dev pod becomes your stable login node — install your dotfiles once, build your environment on the persistent disk, and use it indefinitely

The 720h (30 days) ceiling is just the upper bound on a single reservation. Extend as you go.

### 3. **`prod-east1` — new spot-only region in us-east-1 (⚠️ VERY BETA)**

A second cluster, separate from prod (us-east-2), for spot capacity and future B300 access.

**What's there today (all spot):**

| GPU | Instance |
|---|---|
| B300 | p6-b300.48xlarge |
| B200 | p6-b200.48xlarge |
| H200 | p5e.48xlarge |
| H100 | p5.48xlarge |
| A100 | p4d.24xlarge |
| L4 | g6.12xlarge |
| T4 | g4dn.12xlarge |
| CPU (x86) | c7i.8xlarge |

This is the **first place** we have B300 capacity at all — us-east-2 doesn't have B300 quota.

**How `--spot` works:**

```bash
gpu-dev reserve --gpu-type b300 --gpus 8 --hours 4 --spot
```

- **~1/3 the cost** of on-demand. AWS sets the price dynamically based on free capacity; today p5 spot is typically 60-70% off list, B200/B300 similar.
- **Not guaranteed.** AWS can reclaim the instance with a 2-minute notice when on-demand demand spikes. The Node Termination Handler drains your pod cleanly and your reservation moves to `failed` — your persistent disk survives but the workload is killed mid-run.
- **Best for**: interruption-tolerant training (with checkpoints), dev/exploration, benchmarks, anything where "might restart" is acceptable.
- **Avoid for**: irreplaceable long jobs without checkpointing. Use prod (us-east-2) reserved capacity for those.

**How to switch:**
```bash
pip install --upgrade gpu-dev      # >= 0.5.25
gpu-dev config environment prod-east1
gpu-dev avail                       # us-east-1 cluster
gpu-dev reserve --gpu-type t4 --gpus 1
```

Switch back any time with `gpu-dev config environment prod`.

**⚠️ Things to know before using it:**

- **Beta.** Almost certainly some sharp edges. Report anything weird.
- **Spot lifecycle**: AWS can reclaim a spot node with a 2-minute notice. Your pod will be drained gracefully by NTH and the reservation will fail — your data on persistent disk survives but the workload is killed.
- **Disks live per-cluster.** Your `/home/dev` persistent disk on prod (us-east-2) does NOT exist in prod-east1. You start with a fresh disk in the new region. Cross-region snapshot import is on the roadmap, not in v1.
- **EFS too**: `/shared` is per-region. Different bucket of files.

**When `prod-east1` is the right choice:**
- prod is queued and you don't want to wait
- You want cheaper compute and can tolerate occasional interruptions
- You need B300 (only available here)
- You're benchmarking spot vs reserved, or testing checkpoint-recovery flows

**When it's NOT:**
- Long training runs that can't tolerate interruption — stay on prod's reserved capacity
- You need your existing prod disk/data — copy it manually via rsync first

### 4. **PyTorch 2.12.0 + CUDA 13.2**

Base image updated to `pytorch/pytorch:2.12.0-cuda13.2-cudnn9-devel` (released May 13). CUDA 12.8, 12.9, 13.0, 13.1 also available — switch with `export CUDA_HOME=/usr/local/cuda-12.8`.

## 🔧 Technical Improvements

### IRSA Pod Identity
Pods now run under the `gpu-dev-pod-sa` service account, bound to a minimal IAM role (SQS send, DDB read on reservations + availability, STS). `AWS_ROLE_SESSION_NAME` is set to the user's identity so DDB-side ownership filters keep working unchanged. No more `aws sso login` from inside a pod.

### MULTINODE env vars
Every pod in a multinode reservation gets:
- `MULTINODE_HOSTS` (CSV of headless DNS names)
- `MULTINODE_PEER_PODS`, `MULTINODE_RANK`, `MULTINODE_SIZE`
- `MASTER_ADDR` (= rank 0 host), `MASTER_PORT=29500`
- `MULTINODE_IPS` and `MASTER_IP` resolved at shell start

`torchrun` and `mpirun` work without manual wiring.

### Node Termination Handler (NTH)
DaemonSet on every node listens to AWS's 2-min spot interruption notice and gracefully drains pods. Pods get a clean `SIGTERM` instead of being killed cold. Active on both regions.

## 📚 Updated CLI

```bash
gpu-dev --version            # need >= 0.5.26 for everything
gpu-dev submit --help        # new
gpu-dev config environment   # lists test / prod / prod-east1
```

## 🎯 Next Steps

```bash
pip install --upgrade gpu-dev
```

That's it. Pods auto-upgrade gpu-dev in the background, so once you're on the new CLI everything else flows from there.

## 🐛 Bug Fixes

- IRSA token now readable by the `dev` user (was falling back to the node's IAM role)
- `gpu-dev list` filters by the correct identity inside pods
- `gpu-dev connect` injects `AddKeysToAgent` so subsequent pod→pod hops have the laptop's key forwarded
- Spot ASGs use proper instance_market_options so EKS schedules nodes correctly
- Persistent disks restore cleanly after the previous region drift

## 🙏 Try it out

- New flows: `gpu-dev submit --runtime ./ -- bash run.sh`
- Long-lived dev pod: `gpu-dev reserve --gpu-type cpu-x86 --hours 720 --disk default`
- Spot region: `gpu-dev config environment prod-east1 && gpu-dev avail`

**Thanks**

Huge thanks to **Bob Ren** for stress-testing `gpu-dev submit` end-to-end on multinode H100 with NCCL all-reduce benchmarks — caught a handful of rough edges before this hit the rest of the team.

**Questions or issues?** Open an issue on GitHub or reach out — especially for prod-east1, where we expect to find rough edges.

---
