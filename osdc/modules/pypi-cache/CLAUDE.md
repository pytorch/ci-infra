# modules/pypi-cache/ — Self-Learning PyPI Package Cache

Deployment-based PyPI cache on shared EFS storage. Runs one Deployment per CUDA
version (cpu, cu121, cu124, etc.) with ClusterIP Services. Each pod has two
containers: an nginx caching reverse proxy (front-facing, port 8080) and a
pypiserver backend (gunicorn, port 8081). Each pypiserver instance serves a
dedicated wheelhouse directory and captures access logs for a future self-learning
builder.

## What's here

| Path | Purpose |
|------|---------|
| `deploy.sh` | Reads terraform outputs + clusters.yaml, generates manifests, applies k8s resources |
| `terraform/` | AWS EFS filesystem, mount targets, security group, EFS CSI driver addon, IRSA |
| `kubernetes/namespace.yaml` | `pypi-cache` namespace |
| `kubernetes/kustomization.yaml` | Base k8s resources (namespace, SA) |
| `kubernetes/storageclass.yaml.tpl` | EFS-backed StorageClass template |
| `kubernetes/pvc.yaml.tpl` | ReadWriteMany PVC template |
| `kubernetes/deployment.yaml.tpl` | Per-CUDA pypiserver Deployment template |
| `kubernetes/service.yaml.tpl` | Per-CUDA ClusterIP Service template |
| `kubernetes/serviceaccount.yaml` | SA for pypi-cache pods |
| `scripts/python/generate_manifests.py` | Reads clusters.yaml + templates, outputs StorageClass/PVC/Deployments/Services YAML |
| `scripts/python/log_rotator.py` | Stdin-to-file log rotator with date-based rotation (no longer deployed; nginx logs upstream requests directly) |
| `scripts/python/wants_collector.py` | Log scanner + PyPI filter + S3 uploader (mounted as ConfigMap) |
| `scripts/python/test_wants_collector.py` | Unit tests for wants_collector.py |
| `scripts/python/conftest.py` | pytest path setup for cross-module imports |
| `kubernetes/nginx.conf` | nginx caching proxy config template with njs index merging (`__DNS_RESOLVER__`, `__NGINX_MAX_CACHE_SIZE__` placeholders) |
| `kubernetes/merge_indexes.js` | njs script for merging local pypiserver + upstream pypi.org indexes (resolves BY/BZ index shadowing) |
| `kubernetes/wants-collector-sa.yaml` | ServiceAccount for wants-collector (IRSA-annotated by deploy.sh) |
| `kubernetes/wants-collector-deployment.yaml.tpl` | Wants-collector Deployment template (sed placeholders) |
| `scripts/python/wheel_syncer.py` | S3-to-EFS wheel sync daemon (mounted as ConfigMap) |
| `scripts/python/test_wheel_syncer.py` | Unit tests for wheel_syncer.py |
| `kubernetes/wheel-syncer-sa.yaml` | ServiceAccount for wheel-syncer (IRSA-annotated by deploy.sh) |
| `kubernetes/wheel-syncer-deployment.yaml.tpl` | Wheel-syncer Deployment template (sed placeholders) |
| `kubernetes/nodepool.yaml.tpl` | Karpenter NodePool template (dedicated pypi-cache nodes) |
| `kubernetes/ec2nodeclass.yaml.tpl` | EC2NodeClass template (AWS node config for Karpenter) |
| `terraform/wheel-cache-bucket/` | Standalone tofu root for shared S3 bucket (one-time setup) |
| `docker/Dockerfile` | Reserved for future builder image |

## Configuration (clusters.yaml)

```yaml
defaults:
  pypi_cache:
    namespace: pypi-cache
    server_port: 8080
    internal_port: 8081
    image: pypiserver/pypiserver:v2.4.1
    nginx_image: docker.io/nginxinc/nginx-unprivileged:1.27-alpine
    workers: 4
    replicas: 2
    instance_type: r5d.12xlarge
    storage_request: "1Ti"
    server:
      cpu: "500m"
      memory: "768Mi"
    nginx:
      cpu: 8
      memory_gi: 2
      cache_size: "30Gi"
    target_architectures: ["x86_64", "aarch64"]
    target_manylinux: "2_17"
    log_max_age_days: 30
```

**`cuda_versions` and `python_versions` are NOT module defaults.** They must be
configured in `clusters.yaml` — either under `defaults.pypi_cache` or per-cluster
under `clusters.<id>.pypi_cache`. The module's Python `DEFAULTS` dict does not
contain these keys; omitting them from `clusters.yaml` will result in no CUDA
slugs being generated.

**CUDA slug normalization:** `cuda_slug()` normalizes versions to **major.minor
only**. A `clusters.yaml` entry of `"12.8.1"` produces slug `cu128`, not `cu1281`.
This aligns with the PyTorch convention (`download.pytorch.org/whl/cu128/`) and
with the `setup-pypi-cache` action's auto-detection from `build-environment`
strings (which contain only major.minor, e.g., `cuda12.8`).

Per-cluster overrides work the usual way:

```yaml
clusters:
  arc-cbr-production:
    pypi_cache:
      replicas: 3
    modules:
      - pypi-cache
```

## Dynamic pod sizing

When `instance_type` is set (default: `r5d.12xlarge`), pod resources are computed
dynamically from instance specs — same formula as BuildKit:

1. Look up total vCPU + memory for the instance type
2. Subtract kubelet reserved resources
3. Subtract DaemonSet overhead (300m CPU, 440Mi memory)
4. Apply 10% margin
5. Divide by pods_per_node (= number of CUDA slugs)
6. Within each pod, nginx gets a fixed allocation (`nginx.cpu`, `nginx.memory_gi`);
   pypiserver gets the remainder (bulk of memory is OS page cache for EFS)

This gives Guaranteed QoS (requests == limits) for predictable memory allocation
and effective OS page cache utilization.

Default sizing (r5d.12xlarge, 3 pods/node):
- Total per pod: 14 vCPU, 105 GiB
- nginx container: 8 vCPU, 2 GiB (fixed allocation)
- pypiserver container: 6 vCPU, 103 GiB (remainder)
- Actual CUDA slugs (and therefore pods per node) depend on `cuda_versions` in `clusters.yaml`

When `instance_type` is empty/unset, falls back to manual `server.*` config
values and runs on shared base infrastructure nodes.

## Architecture

```
Job pods set PIP_INDEX_URL per CUDA version:
  http://pypi-cache-cu121.pypi-cache.svc.cluster.local:8080/whl/cu121/

            +--- ClusterIP Service ----+
            |  pypi-cache-cu121:8080   |
            +-----------+--------------+
                        |
          +-------------+-------------+
          |             |             |
    +-----v-----+ +----v------+ +---v-------+
    | Pod (r0)  | | Pod (r1)  | | Pod (rN)  |
    |           | |           | |           |
    | +-------+ | | +-------+ | | +-------+ |
    | | nginx | | | | nginx | | | | nginx | |
    | | :8080 | | | | :8080 | | | | :8080 | |
    | +---+---+ | | +---+---+ | | +---+---+ |
    |     |     | |     |     | |     |     |
    | +---v---+ | | +---v---+ | | +---v---+ |
    | |pypisrv| | | |pypisrv| | | |pypisrv| |
    | | :8081 | | | | :8081 | | | | :8081 | |
    | +-------+ | | +-------+ | | +-------+ |
    +-----+-----+ +----+------+ +---+-------+
          |             |            |
          +-------------+------------+
                        |
          +-------------v------------+
          |  PVC: pypi-cache-data    |
          |  ReadWriteMany (EFS)     |
          +--------------------------+

Traffic flow: Service (:8080) -> nginx (:8080) -> pypiserver (:8081)

One Deployment + Service per CUDA version:
  pypi-cache-cpu, pypi-cache-cu121, pypi-cache-cu124
```

## Storage layout on EFS

```
/pypi-cache/
  wheelhouse/
    cpu/           # pure-Python + CPU-only wheels
    cu121/         # CUDA 12.1 wheels
    cu124/         # CUDA 12.4 wheels
  logs/
    upstream/      # nginx fallback logs (fallback.log)
```

## Request flow

### PyPI packages (default)

1. Composite GitHub Action sets `PIP_EXTRA_INDEX_URL` / `UV_INDEX` for the job
2. pip/uv queries `pypi-cache-{slug}.pypi-cache.svc.cluster.local:8080/simple/{package}/`
3. nginx's `/simple/` location invokes `merge_indexes.mergeSimple` (njs)
4. The njs handler issues two subrequests in parallel:
   - `/_internal/local/simple/{package}/` — proxied to pypiserver (local wheelhouse)
   - `/_internal/upstream/simple/{package}/` — proxied to pypi.org
5. The handler merges both index responses: deduplicates by filename (local wins
   on collision), rewrites upstream absolute URLs to relative paths, and returns
   the combined index to the client
6. If only one source returns 200, that response is used (with URL rewriting for
   upstream). If both fail, nginx returns 404.
7. pip selects the best matching distribution from the merged index
8. pip requests the download URL — routed by nginx to pypiserver (flat paths)
   or `files.pythonhosted.org` (hash-based `/packages/` paths)
9. nginx caches index responses (local 5m, upstream 10m) and downloads (1 month)
10. Upstream fallback requests are logged to EFS for the wants-collector

#### merge_indexes.js (njs)

The njs script (`kubernetes/merge_indexes.js`) resolves the index shadowing
problem where pypiserver returns HTTP 200 with wrong-variant wheels, preventing
the upstream PyPI index from being consulted. By merging both indexes, clients
always see the full set of available packages regardless of which variants are
in the local wheelhouse. Supports both PEP 503 HTML and PEP 691 JSON formats.
See `docs/nginx-routing-analysis.md` for the full scenario analysis.

### PyTorch packages (download.pytorch.org)

1. Composite GitHub Action sets `PIP_INDEX_URL` / `UV_DEFAULT_INDEX` to
   `http://pypi-cache-{slug}.pypi-cache.svc.cluster.local:8080/whl/{cuda}/`
2. pip queries the `/whl/{cuda}/` index — nginx proxies directly to
   `download.pytorch.org` (no pypiserver involved) and caches index pages (10m)
3. **nginx rewrites absolute `https://download.pytorch.org/...` URLs in
   index responses to relative `/whl/...` paths** (`sub_filter`)
4. pip requests `/whl/{cuda}/torch-xxx.whl` — nginx proxies to
   `download.pytorch.org` and caches the wheel (1 month, immutable)
5. cache-enforcer blocks direct access to `download.pytorch.org` on runner nodes,
   forcing all traffic through this proxy

### nginx caching behavior

- **Full-path caching**: nginx caches ALL responses — both pypiserver (local wheels)
  and upstream (pypi.org, download.pytorch.org). Cache keys use `local:` and
  `upstream:` prefixes to prevent collision between sources for the same URI
- **Index caching (njs subrequests)**: The `/simple/` location uses njs to merge
  local and upstream indexes. Each subrequest is cached independently:
  local responses cached 5m (`local:` prefix), upstream responses cached 10m
  (`upstream:` prefix). Key includes `$http_accept` for PEP 691 content negotiation
- **pypiserver wheel caching**: `.whl`, `.tar.gz`, `.zip` downloads cached 30d
  (wheels are immutable once built). Key: `local:$request_uri`
- **proxy_cache_lock**: Only one request per cache key hits the backend; concurrent
  duplicates wait for the first response (thundering herd protection, 10s timeout)
- **PEP 691 content negotiation**: The `Accept` header is included in the cache key
  so `application/vnd.pypi.simple.v1+json` and `text/html` responses are cached separately
- **sub_filter URL rewriting**: Absolute `https://files.pythonhosted.org` and
  `https://download.pytorch.org` URLs in upstream responses are rewritten to relative
  paths (`/packages/...` and `/whl/...` respectively) so pip downloads go through the
  proxy (required for cache-enforcer compatibility)
- **`/packages/` proxy**: Rewritten download URLs are proxied to `files.pythonhosted.org`
  and cached for 1 month (packages are immutable). Cache key is `$request_uri` only
  (no Accept header for binary downloads)
- **`/whl/` proxy**: PyTorch index and downloads proxied to `download.pytorch.org`.
  Index pages cached 10m, wheel files cached 1 month (immutable)
- **proxy_cache_use_stale**: Serves stale cached responses when pypiserver is
  temporarily unavailable (error, timeout, updating)
- **Cache storage**: On NVMe instances (r5d.12xlarge), ~570 GiB hostPath per pod
  (`/mnt/k8s-disks/0/nginx-cache-{slug}`); Karpenter auto-formats NVMe as RAID0.
  On non-NVMe instances, falls back to 30 GiB emptyDir. Both are ephemeral (survive
  container restarts but not pod rescheduling)

## Self-learning loop

The self-learning loop has four stages, each handled by a separate component:

1. Job downloads packages via proxy (logged by nginx)
2. Wants-collector identifies packages needing builds, uploads wants
   list to S3
3. Builder (external workflow) builds wheels, uploads to S3 under
   `{slug}/`
4. Wheel syncer downloads built wheels from S3 to EFS wheelhouse
5. Next job gets pre-built wheel — instant install

## Wants collector

The wants-collector is the first step of the self-learning loop. It runs as a
single-replica Deployment (`pypi-wants-collector`) in the `pypi-cache` namespace,
scanning nginx fallback logs on EFS to determine which packages are requested.

### How it works

Each cycle (every 2 minutes):
1. Scans nginx fallback logs in `/data/logs/upstream/` (EFS, read-only mount)
2. Extracts unique `(package, version)` pairs from download requests
3. Downloads the shared prebuilt cache from S3 (`prebuilt-cache.txt`)
4. For packages not in the cache, queries the PyPI JSON API to check wheel
   availability
5. Filters: packages with compatible pre-built wheels for the full target matrix
   are added to the prebuilt cache. Packages missing any wheel are added to the
   wants list.
6. Uploads `wants/<cluster-id>.txt` and `prebuilt-cache.txt` to S3
7. Touches `/tmp/last-success` for liveness probe, then sleeps

### PyPI filtering

The target matrix is defined in `clusters.yaml` under `pypi_cache`:
- `python_versions`: Python versions to check (e.g., 3.10, 3.11, 3.12)
- `target_architectures`: CPU architectures (e.g., x86_64, aarch64)
- `target_manylinux`: Minimum manylinux version (e.g., 2_17 = glibc 2.17)

A package is excluded from the wants list if:
- It has a `py3-none-any` wheel (pure Python)
- Compatible manylinux wheels exist for every (python x arch) combination
- It returns 404 from PyPI (unknown/internal package)

A package is included if any combination is missing a compatible wheel.

### S3 bucket layout

```
s3://pytorch-pypi-wheel-cache/
├── wants/
│   ├── arc-staging.txt
│   ├── arc-production.txt
│   └── arc-cbr-production.txt
├── prebuilt-cache.txt
├── needbuild.txt
├── cpu/                         # built wheels (synced to EFS by wheel-syncer)
│   └── {package}-{version}-{tags}.whl
├── cu121/
│   └── {package}-{version}-{tags}.whl
└── cu128/
    └── {package}-{version}-{tags}.whl
```

- `wants/<cluster-id>.txt` — per-cluster, expires after 7 days, contains
  `package==version` entries
- `prebuilt-cache.txt` — shared across clusters, no expiry, grows monotonically
- `needbuild.txt` — manually managed, no expiry, read-only by collector (see below)
- `{slug}/*.whl` — built wheels uploaded by the builder, synced to EFS by
  wheel-syncer

### Manual build override (`needbuild.txt`)

`needbuild.txt` is a manually-managed file listing package names (one per line, no
versions) that should always be placed into the wants list when they appear in access
logs, bypassing both the prebuilt cache and the PyPI availability check.

Use this to force building packages that have pre-built wheels on PyPI but need
custom variants (e.g., CUDA-specific builds, custom compiler flags). The collector
reads this file each cycle but never writes to it.

Format:
```
# Comments start with #
# One PEP 503-normalized package name per line (no version)
torch
triton
flash-attn
```

Behavior:
- Package in `needbuild` AND in access logs → added to wants unconditionally
- Package in `needbuild` but NOT in access logs → ignored (only requested packages)
- Package in `needbuild` AND in `prebuilt-cache.txt` → needbuild wins
- File missing or empty → no effect (backward compatible)

### One-time S3 bucket setup

The S3 bucket is a shared resource created once (not per-cluster). To create it:

```bash
cd modules/pypi-cache/terraform/wheel-cache-bucket
tofu init
tofu apply
```

### Known limitations

- nginx cache hits are invisible to the wants-collector. Acceptable: every cached
  response was a cache miss at some point, so it was logged on first request.
- Fallback logs rotate daily (`fallback.YYYY-MM-DD.log` via nginx `map $time_iso8601`).
  The wants-collector deletes files older than 30 days (configurable via `log_max_age_days`).
- PyTorch wheels (`/whl/` path) are proxied directly by nginx to
  `download.pytorch.org` and never reach pypiserver. Excluded from wants list.
- Packages not on PyPI are silently skipped (can't be built from PyPI sources).
- Prebuilt cache is shared across clusters — concurrent updates may cause a
  redundant PyPI lookup on the next cycle (no data loss, S3 PutObject is atomic).

## Wheel syncer

The wheel syncer is the final step of the self-learning loop. It runs as a
single-replica Deployment (`pypi-wheel-syncer`) in the `pypi-cache` namespace,
syncing built wheel packages from S3 to the EFS wheelhouse so pypiserver can
serve them.

### How it works

Each cycle (every 60 seconds):
1. For each configured CUDA slug (cpu, cu121, cu128, etc.):
   a. Lists `.whl` files in `s3://pytorch-pypi-wheel-cache/{slug}/`
   b. Compares against local wheelhouse directory `/data/wheelhouse/{slug}/`
   c. Downloads any missing wheels
2. Touches `/tmp/last-success` for liveness probe, then sleeps

Downloads use atomic writes: files are written to `{name}.whl.tmp` first, then
renamed to `{name}.whl`. This prevents pypiserver from serving partial files.

pypiserver uses the `simple-dir` backend, which re-scans the wheelhouse directory
on every request. New wheels are available immediately after download (subject to
nginx's 5-minute index cache TTL).

### IRSA permissions

The wheel syncer has **read-only** S3 access (`s3:GetObject`, `s3:ListBucket`).
It never writes to S3 — the builder is responsible for uploading wheels.

## Log capture

nginx logs upstream-bound requests (fallbacks to pypi.org and downloads from
files.pythonhosted.org) to `/data/logs/upstream/fallback.YYYY-MM-DD.log` on EFS
(daily rotation via `map $time_iso8601`). The wants-collector reads all `*.log`
files in this directory and deletes files older than 30 days after each cycle.

pypiserver runs without the `-v` flag; its stdout goes only to Kubernetes/Alloy
(no EFS log files).

## Deploy ordering

1. Terraform creates EFS filesystem, mount targets, security group, CSI driver addon, IRSA roles
2. `deploy.sh` reads terraform outputs (EFS filesystem ID, IRSA role ARNs)
3. Kustomize applies base resources (namespace, ServiceAccounts)
4. Annotates both SAs with IRSA role ARNs (wants-collector and wheel-syncer)
5. Creates all ConfigMaps (nginx-config, wants-collector-scripts, wheel-syncer-scripts)
6. `generate_manifests.py` produces StorageClass, PVC, Deployments, Services, NodePools
7. Manifests applied in dependency order: StorageClass -> PVC -> **NodePools** -> Services -> Deployments
8. Restarts pypiserver Deployments, waits for rollouts
9. Applies wants-collector Deployment (sed template substitution), restarts, waits for rollout
10. Applies wheel-syncer Deployment (sed template substitution), restarts, waits for rollout

## Dependencies

- Base must be deployed (EKS cluster, VPC, subnets)
- Terraform creates EFS + CSI driver (runs before k8s manifests)
- Pods run on dedicated r5d.12xlarge nodes (Karpenter NodePool `pypi-cache`, taint `workload=pypi-cache:NoSchedule`)
- When `instance_type` is unset, falls back to base infrastructure nodes (`CriticalAddonsOnly` taint)

## Portability boundary

`terraform/` is the ONLY cloud-specific code (AWS EFS, security groups, CSI driver).
All Kubernetes manifests are portable — the StorageClass is the sole integration point
between cloud storage and k8s workloads.
