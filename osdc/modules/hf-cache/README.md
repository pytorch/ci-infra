# hf-cache — shared HuggingFace model cache

Gives OSDC runners a shared, read-only HuggingFace model cache at `/mnt/hf_cache`,
the OSDC equivalent of the old EC2 CI `/mnt/hf_cache` mount. Jobs read model
weights from a local cache instead of downloading from the Hub on every run.

## Design in one paragraph

The model cache is stored as **plain, symlink-free HuggingFace cache-layout files
in S3** (the portable source of truth — any object store can host the same layout).
Each cluster gets its **own private bucket** (`pytorch-hf-model-cache-<cluster_id>`)
in the cluster's region, created by the module's own per-cluster terraform — so
`just deploy-module <cluster> hf-cache` provisions everything with no separate
bucket step, reads stay in-region, and there's no cross-cluster write contention to
reason about. A privileged per-node **rclone FUSE mount** (`mount-daemonset`)
exposes the bucket **read-only** at the host path `/mnt/hf_cache`; reads are lazy
and cached on node-local NVMe, so a cold Karpenter node only pulls the models its
jobs touch. Job pods (ARC kubernetes mode) get the path bind-mounted via the gated
`# BEGIN_HF_CACHE` block in `modules/arc-runners/templates/runner.yaml.tpl`. A
**refresh CronJob** is the only writer: it downloads the curated model set and
publishes a symlink-free copy to `s3://<bucket>/hub`.

No metadata engine, no EFS — just S3 + rclone, which keeps it cloud-portable.

## Components

| Component | What it does |
|-----------|--------------|
| `terraform/` | Per-cluster private S3 bucket `pytorch-hf-model-cache-<cluster_id>` + IRSA roles `hf-cache-mount` (read-only) and `hf-cache-refresh` (read/write). Applied automatically by `deploy-module`. |
| `kubernetes/mount-daemonset.yaml.tpl` | rclone FUSE mount → read-only `/mnt/hf_cache` on every runner/workflow node |
| `kubernetes/refresh-cronjob.yaml.tpl` | Downloads `models.txt` from the Hub, publishes symlink-free to S3 |
| `scripts/python/refresh_cache.py` | Refresh driver (download + `rclone copy -L`, dropping `blobs/`) |
| `models.txt` | Curated model manifest (kept in sync with pytorch/pytorch CI pins) |

## Runner consumption

When `hf-cache` is in a cluster's `modules:` list, `generate_runners.py` keeps the
`# BEGIN_HF_CACHE` block, which adds to every job pod:

- volume + read-only `hostPath` mount of `/mnt/hf_cache` (`HostToContainer` propagation)
- env: `HF_HOME=/mnt/hf_cache`, `HF_HUB_CACHE=/mnt/hf_cache/hub`, `HF_HUB_OFFLINE=1`,
  `TRANSFORMERS_OFFLINE=1`, `HF_DATASETS_OFFLINE=1`

`from_pretrained(...)` / `vllm.LLM(model=...)` then resolve from the cache with no
code changes. When the module is absent the block is stripped, so this is a no-op
for clusters that don't enable it.

## Enable on a cluster

1. Add `hf-cache` to the cluster's `modules:` list in `clusters.yaml` (after
   `arc-runners`), then deploy — this provisions the bucket + IRSA, the mount
   DaemonSet, and the refresh CronJob:
   ```
   just deploy-module <cluster> hf-cache
   just deploy-module <cluster> arc-runners   # re-render job pods with the HF_CACHE block
   ```
2. (Optional) create the gated/private-model token Secret, then re-run the refresh:
   ```
   kubectl create secret generic hf-cache-token -n hf-cache --from-literal=token=hf_xxx
   ```
3. Populate the cache immediately (otherwise it waits for the CronJob):
   ```
   kubectl create job -n hf-cache --from=cronjob/hf-cache-refresh hf-cache-refresh-manual
   ```

## Open items (see PR description)

- **Symlink-free layout** is assumed to resolve transparently via `from_pretrained`
  from an `rclone -L`, `blobs/`-excluded layout — needs a validation spike before
  enabling on a real cluster.
- The mount DaemonSet is **privileged** (FUSE + Bidirectional propagation); confirm
  this is acceptable under the cluster's Pod Security posture.
- Each cluster has its own bucket, so there's no cross-cluster write contention and
  reads stay in-region. The trade-off is no cross-cluster dedup — every cluster
  downloads and stores its own copy of the models (acceptable; S3 storage is cheap).
- Strict-offline (`HF_HUB_OFFLINE=1`): an uncached model errors out (matches EC2).
  Graceful online fallback (overlay) is a possible enhancement.
