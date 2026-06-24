# hf-cache — shared HuggingFace model cache

Gives OSDC runners a shared, read-only HuggingFace model cache at `/mnt/hf_cache`,
the OSDC equivalent of the old EC2 CI `/mnt/hf_cache` mount. Jobs read model
weights from a local cache instead of downloading from the Hub on every run.

## Design in one paragraph

The model cache is stored as **plain, symlink-free HuggingFace cache-layout files
in a per-region S3 bucket** (the portable source of truth — any object store can
host the same layout). There is one bucket per region
(`pytorch-hf-model-cache-<region>`), shared by the clusters in that region, and
each cluster uses its **own prefix** within it
(`s3://pytorch-hf-model-cache-<region>/<cluster_id>/hub`), so the per-cluster
refresh writers never contend over the same keys — mirroring how `pypi-cache`
partitions per-cluster writes with `wants/<cluster_id>.txt`. A same-region bucket
also means runners read without cross-region S3 egress. A privileged per-node **rclone
FUSE mount** (`mount-daemonset`) exposes that prefix **read-only** at the host path
`/mnt/hf_cache`; reads are lazy and cached on node-local NVMe, so a cold Karpenter
node only pulls the models its jobs touch. Job pods (ARC kubernetes mode) get the
path bind-mounted via the gated `# BEGIN_HF_CACHE` block in
`modules/arc-runners/templates/runner.yaml.tpl`. A **refresh CronJob** is the only
writer for its cluster's prefix: it downloads the curated model set and publishes a
symlink-free copy to S3.

No metadata engine, no EFS — just S3 + rclone, which keeps it cloud-portable.

## Components

| Component | What it does |
|-----------|--------------|
| `terraform/hf-cache-bucket/` | Per-region private S3 bucket `pytorch-hf-model-cache-<region>` (one-time, via `just hf-cache-bucket <region>`) |
| `terraform/` | Per-cluster IRSA roles: `hf-cache-mount` (read-only), `hf-cache-refresh` (read/write) |
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

1. One-time (per region): provision the model-cache bucket for the cluster's region
   ```
   just hf-cache-bucket <region>     # e.g. us-east-1; safe to skip if already done for that region
   ```
2. (Optional) create the gated/private-model token Secret:
   ```
   kubectl create secret generic hf-cache-token -n hf-cache --from-literal=token=hf_xxx
   ```
3. Add `hf-cache` to the cluster's `modules:` list in `clusters.yaml` (after
   `arc-runners`), then redeploy:
   ```
   just deploy-module <cluster> hf-cache
   just deploy-module <cluster> arc-runners   # re-render job pods with the HF_CACHE block
   ```
4. Populate the cache immediately (otherwise it waits for the CronJob):
   ```
   kubectl create job -n hf-cache --from=cronjob/hf-cache-refresh hf-cache-refresh-manual
   ```

## Open items (see PR description)

- **Symlink-free layout** is assumed to resolve transparently via `from_pretrained`
  from an `rclone -L`, `blobs/`-excluded layout — needs a validation spike before
  enabling on a real cluster.
- The mount DaemonSet is **privileged** (FUSE + Bidirectional propagation); confirm
  this is acceptable under the cluster's Pod Security posture.
- Multiple clusters are isolated by prefix (`<cluster_id>/hub`) within a per-region
  bucket, so writes don't contend and reads stay in-region. Same-region clusters
  still each store their own copy under their prefix; collapsing that to one shared
  prefix per region (single designated writer) is a possible later optimization.
- Strict-offline (`HF_HUB_OFFLINE=1`): an uncached model errors out (matches EC2).
  Graceful online fallback (overlay) is a possible enhancement.
