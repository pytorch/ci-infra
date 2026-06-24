# hf-cache — shared HuggingFace model cache

Read-mostly HuggingFace model cache at `/mnt/hf_cache` for OSDC runners — the
OSDC equivalent of the old EC2 `/mnt/hf_cache` mount.

## Design

Each cluster gets its own private S3 bucket (`pytorch-hf-model-cache-<cluster_id>`,
in the cluster's region), provisioned by the module's terraform. A privileged
per-node **rclone FUSE mount** (`mount-daemonset`) exposes it **read-write** at
host `/mnt/hf_cache`; reads are lazy and cached on node-local NVMe (LRU), so a
cold node only pulls the models its jobs touch. Job pods (ARC kubernetes mode)
get the path bind-mounted via the gated `# BEGIN_HF_CACHE` block in
`modules/arc-runners/templates/runner.yaml.tpl`.

Writes follow the existing pytorch CI model: the cache is writable, but jobs run
offline (read-only) except on `ci-refresh-hf-cache` runs (triggered by trusted,
write-access contributors / model-pin bumps), which go online and write new
models through to S3. The **workflow** owns that offline gating — the module just
provides the writable mount. No separate writer/refresh job.

No metadata engine, no EFS — just S3 + rclone, which keeps it cloud-portable.
HF writes land symlink-free (rclone mounts don't support symlinks, so
huggingface_hub falls back to plain files), so the S3 layout stays portable.

## Components

| Component | What it does |
|-----------|--------------|
| `terraform/` | Per-cluster private bucket + a read-write IRSA role (`hf-cache-mount` SA). Applied by `deploy-module`. |
| `kubernetes/mount-daemonset.yaml.tpl` | rclone FUSE mount → `/mnt/hf_cache` on every runner/workflow node |
| `deploy.sh` | Annotates the SA with the role and rolls out the DaemonSet |

## Runner consumption

When `hf-cache` is in a cluster's `modules:`, `generate_runners.py` keeps the
`# BEGIN_HF_CACHE` block, which adds to every job pod a `/mnt/hf_cache` hostPath
mount (`HostToContainer`) and `HF_HOME`/`HF_HUB_CACHE`. Offline flags are left to
the workflow. When the module is absent the block is stripped (no-op).

## Enable on a cluster

```
# add "hf-cache" to the cluster's modules: list (after arc-runners), then:
just deploy-module <cluster> hf-cache
just deploy-module <cluster> arc-runners   # re-render job pods with the mount
```

## Open items (see PR description)

- **Symlink-free layout**: confirm `from_pretrained`/vLLM read back correctly from
  the (symlink-free, copy-fallback) layout HF writes over rclone.
- The mount DaemonSet is **privileged** (FUSE + Bidirectional propagation);
  confirm it's acceptable under the cluster's Pod Security posture.
- Writable shared mount: writes are gated only by the workflow offline flag (a
  policy control), not by infra — acceptable given writers are trusted
  (write-access contributors). The RO-mount + dedicated-refresh alternative trades
  that for infra-enforced confinement.
