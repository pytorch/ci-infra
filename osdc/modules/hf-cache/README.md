# hf-cache — shared HuggingFace model cache

Read-only HuggingFace model cache at `/mnt/hf_cache` for OSDC runners — the OSDC
equivalent of the old EC2 `/mnt/hf_cache` mount.

## Design

Each cluster has its own private S3 bucket (`pytorch-hf-model-cache-<cluster_id>`,
in the cluster's region), provisioned by the module's terraform. A privileged
per-node **rclone FUSE mount** (`mount-daemonset`) exposes it **read-only** at
host `/mnt/hf_cache`; reads are lazy and cached on node-local NVMe (LRU), so a
cold node only pulls the models its jobs touch. Job pods (ARC kubernetes mode)
get the path bind-mounted read-only via the gated `# BEGIN_HF_CACHE` block in
`modules/arc-runners/templates/runner.yaml.tpl`.

rclone's memory is **reserved** (`request == limit`) and **tiered by GPU count**
(`karpenter.k8s.aws/instance-gpu-count`), since RSS scales with job concurrency:
`deploy.sh` renders one DaemonSet per tier — 8-GPU → 4Gi, 4-GPU → 2Gi, 2-GPU → 1Gi,
1-GPU → 512Mi, and the CPU catch-all → 256Mi. See `MOUNT_TIERS` in `deploy.sh`.

To keep RSS inside those reserves the mount runs with `--buffer-size 0` (no
per-open-file in-RAM read-ahead — redundant under `--vfs-cache-mode full`, which
serves from the on-disk cache) and `--use-mmap` (frees buffers back to the OS).
Without them, concurrent large-model (safetensors) reads OOM-kill rclone, and the
dead FUSE mount surfaces as a spurious `LocalEntryNotFoundError` in the job.

**Writes are gated by GitHub OIDC, not by the mount.** Job pods can't write the
cache (read-only mount, read-only IRSA). On `ci-refresh-hf-cache` runs, the
pytorch/pytorch workflow assumes a GitHub-OIDC role
(`gha_workflow_hf-cache-write` in pytorch-gha-infra, env-gated to
`repo:pytorch/pytorch:environment:hf-cache-write`) and `aws s3 sync`s the
downloaded models to the bucket. AWS enforces the gate, so only approved refresh
runs — not arbitrary/fork PRs — can write.

No metadata engine, no EFS — just S3 + rclone, cloud-portable. `aws s3 sync`
follows symlinks, so the S3 layout is symlink-free and portable.

## Components

| Component | What it does |
|-----------|--------------|
| `terraform/` | Per-cluster private bucket + read-only IRSA role (`hf-cache-mount` SA) |
| `kubernetes/mount-daemonset.yaml.tpl` | rclone read-only FUSE mount → `/mnt/hf_cache` on every runner/workflow node |
| `deploy.sh` | Annotates the SA with the role and rolls out the per-GPU-count mount DaemonSets (`MOUNT_TIERS`) |
| (pytorch-gha-infra) | `gha_workflow_hf-cache-write` OIDC role — the only writer |

## Runner consumption

When `hf-cache` is in a cluster's `modules:`, `generate_runners.py` keeps the
`# BEGIN_HF_CACHE` block, adding to every job pod a read-only `/mnt/hf_cache`
mount, `HF_HOME` (the hub cache derives as `$HF_HOME/hub`), and
`HF_CACHE_S3_BUCKET`/`HF_CACHE_S3_REGION`
(the refresh workflow's write target). When the module is absent the block is
stripped (no-op).

## Workflow contract (pytorch/pytorch ci-refresh-hf-cache)

A refresh run should:
1. Declare `permissions: id-token: write` and `environment: hf-cache-write`.
2. Download models to a writable dir (not the read-only `/mnt/hf_cache`).
3. `aws-actions/configure-aws-credentials` with the
   `gha_workflow_hf-cache-write` role, then
   `aws s3 sync <dir> s3://$HF_CACHE_S3_BUCKET/hub --region $HF_CACHE_S3_REGION`.

## Enable on a cluster

```
# add "hf-cache" to the cluster's modules: list, then:
just deploy-module <cluster> hf-cache
just deploy-module <cluster> arc-runners   # re-render job pods with the mount
```
