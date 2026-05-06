# PyPI Package Cache for CI Runners

## Overview

Every CI job creates a fresh virtualenv and pulls Python packages. Without a local
cache this means slow downloads, PyPI rate limiting, and repeated source builds for
packages without pre-built wheels for our target platform (e.g. CUDA extensions).

The pypi-cache module (`modules/pypi-cache/`) addresses this with a per-cluster
deployment that:

- Serves a PEP 503/691 simple index that merges a local wheelhouse with upstream PyPI
- Proxies and caches wheel downloads from `pypi.org`/`files.pythonhosted.org` and
  `download.pytorch.org` on local NVMe
- Hosts pre-built wheels for packages that lack wheels on PyPI (CUDA extensions,
  niche source-only packages) on shared EFS storage
- Self-discovers which packages need building by scanning its own access logs and
  uploading a wants list to S3, where an out-of-cluster builder consumes it

The cache is paired with the `cache-enforcer` DaemonSet, which iptables-blocks
direct egress from runner nodes to `pypi.org`, `files.pythonhosted.org`, and
`download.pytorch.org` — so all pip/uv traffic on runners is routed through
pypi-cache by force, not by env var alone.

> Detailed operational notes (slug naming, NVMe sizing math, IRSA roles, log
> rotation, etc.) live in the `osdc-pypi-cache` skill. This document covers the
> architecture and the load-bearing invariants that are easy to break.

## Components

The module deploys five distinct workloads per cluster, all in the `pypi-cache`
namespace.

### 1. `pypi-cache-{slug}` Deployments (one per CUDA slug)

A separate Deployment + ClusterIP Service is generated for each entry in
`pypi_cache.cuda_versions` plus a mandatory `cpu` slug. Defaults from
`clusters.yaml` produce slugs `cpu`, `cu126`, `cu128`, `cu130`.

Each pod runs three containers:

- **nginx** (`docker.io/nginxinc/nginx-unprivileged:1.27-alpine`) on port 8080 —
  the only port exposed via Service. Runs as caching reverse proxy and njs index
  merger.
- **pypiserver** (`pypiserver/pypiserver:v2.4.1`) on localhost port 8081 with
  `--backend cached-dir`. Serves locally-built wheels from the per-slug EFS
  wheelhouse subdirectory. The `cached-dir` backend rescans on directory mtime
  change, so wheels added by `wheel-syncer` become servable without restart.
- **nginx-prometheus-exporter** (`docker.io/nginx/nginx-prometheus-exporter:1.4.1`)
  on port 9113 scraping `/stub_status` for monitoring.

Replicas: default 2, override per-cluster (`arc-staging: 1`,
`arc-cbr-production: 5`). Pods spread across nodes via `podAntiAffinity` on
`kubernetes.io/hostname`.

Pods are scheduled on dedicated Karpenter nodes (`workload=pypi-cache:NoSchedule`
taint, default `r5d.12xlarge`). Per-pod CPU/memory is computed from instance
specs in `compute_pod_resources()` for Guaranteed QoS — nginx gets a fixed
`4 vCPU / 64 GiB` slice (sized for njs subrequest buffers under load) and
pypiserver gets the remainder.

### 2. `pypi-wants-collector` Deployment (1 replica)

Long-lived pod running `scripts/python/wants_collector.py`. Each cycle (default
every 120s):

1. Scans EFS access logs at `/data/logs/upstream/fallback.YYYY-MM-DD.log`
2. Downloads the shared `prebuilt-cache.txt` from S3 (header-versioned by the
   target matrix; mismatch invalidates the entire cache)
3. Downloads `needbuild.txt` from S3 (manual override list)
4. For each package not yet in the prebuilt cache, queries PyPI's JSON API to
   check whether a wheel covering the full
   `(python_versions × architectures × manylinux)` matrix already exists
5. Writes packages still needing builds to `s3://pytorch-pypi-wheel-cache/wants/{cluster_id}.txt`
6. Updates `prebuilt-cache.txt` with newly verified packages
7. Deletes log files older than `--max-log-age-days` (default 30)

The collector is the **only S3 writer** in the loop.

### 3. `pypi-wheel-syncer` Deployment (1 replica)

Long-lived pod running `scripts/python/wheel_syncer.py`. Each cycle (default
every 60s) lists `s3://pytorch-pypi-wheel-cache/{slug}/*.whl` for every
configured slug, downloads anything missing from the local EFS wheelhouse using
atomic rename for safe placement.

This is the **only S3 reader for wheels** in the cluster — pods do not pull
wheels from S3 directly.

### 4. External wheel-build pipeline

Lives outside this repository. Reads `wants/{cluster}.txt` and `needbuild.txt`
from S3, builds wheels for the configured matrix, and pushes them to
`s3://pytorch-pypi-wheel-cache/{slug}/*.whl`. The `wheel-syncer` then surfaces
them on EFS within ~60s.

### 5. `cache-enforcer` DaemonSet (separate module)

`modules/cache-enforcer/` runs on `workload-type: github-runner` nodes (NOT on
pypi-cache nodes). Loads the `xt_string` kernel module and installs iptables
REJECT rules in OUTPUT and FORWARD chains that match domain strings in TLS
ClientHello SNI fields and HTTP Host headers for `pypi.org`,
`files.pythonhosted.org`, and `download.pytorch.org`.

This is what forces pip/uv traffic through pypi-cache. **If pypi-cache is
unhealthy, all pip installs on runners fail — there is no bypass.**

## Storage Layout

Two distinct storage layers with different semantics:

| Layer | Type | Lifetime | Contents |
|-------|------|----------|----------|
| Wheelhouse | EFS PVC `pypi-cache-data` (RWX) | Persistent across rescheduling | Built `.whl` files synced from S3 (`/data/wheelhouse/{slug}/`); fallback access logs (`/data/logs/upstream/`) |
| nginx cache | NVMe hostPath `/mnt/k8s-disks/0/nginx-cache-{slug}` (or emptyDir fallback) | Ephemeral; gone on pod rescheduling | Cached PEP 503/691 index responses; cached wheel downloads from PyPI/PyTorch fallback |

The EFS PVC is `ReadWriteMany` with StorageClass `efs-pypi-cache` (provisioner
`efs.csi.aws.com`, `basePath: /pypi-cache`, `reclaimPolicy: Retain`). It's
mounted by every pypi-cache pod, plus the wants-collector and wheel-syncer.

NVMe size per pod is computed as `floor(nvme_gib * 0.95 / pods_per_node)`. For
`r5d.12xlarge` (~1,800 GiB NVMe RAID0) with 4 slugs that's ~427 GiB per pod.
Adding CUDA versions shrinks it. `inactive=7d` on the nginx cache key zone evicts
unused entries regardless of TTL.

The EFS CSI driver is installed by this module's Terraform via
`aws_eks_addon` — not a base infra concern.

### S3 bucket layout

Bucket: `s3://pytorch-pypi-wheel-cache/` (single shared bucket across all
clusters; managed in `terraform/wheel-cache-bucket/`):

| Path | Scope | Writer | Reader |
|------|-------|--------|--------|
| `wants/{cluster_id}.txt` | per-cluster (7-day lifecycle expiry) | wants-collector | external builder |
| `prebuilt-cache.txt` | shared, monotonic | wants-collector | external builder, wants-collector |
| `needbuild.txt` | shared, manual | human via `aws s3 cp` | external builder, wants-collector |
| `{slug}/*.whl` | shared per-slug | external builder | wheel-syncer |

The metadata files (`wants/*`, `prebuilt-cache.txt`, `needbuild.txt`) are
public-read so the external builder doesn't need IAM. Wheels are private.

## Index Routing

The single Service exposes one HTTP port (`8080`) with several path prefixes
handled by different nginx locations.

### `/simple/` and `/simple/{pkg}/` — njs merge handler

Routed to `merge_indexes.js` via `js_content`. The handler issues two
subrequests in parallel:

- `/_internal/local/simple/...` → pypiserver (local wheelhouse contents)
- `/_internal/upstream/simple/...` → `https://pypi.org/simple/...`

Both responses are parsed (HTML for PEP 503, JSON for PEP 691, with HTML→JSON
fallback for pypiserver v2.x which doesn't support PEP 691). The result lists
are deduplicated by filename — local entries win on collision — and rendered
back to the client in the format the client requested.

This **resolves the BY/BZ index shadowing problem**: pypiserver previously
returned 200 for some packages with wrong-variant wheels, which short-circuited
nginx's `proxy_intercept_errors` fallback to PyPI. Merging guarantees both
sources are always considered.

The root listing `/simple/` is passed through to upstream (pypiserver's root
listing is incomplete by design).

### `/whl/...` — PyTorch wheel index

Proxies to `https://download.pytorch.org/whl/...` with sub_filter rewriting and
caching. **This is NOT the locally-built CUDA wheelhouse** — those live behind
the per-slug Service and are served via `/simple/`. The `/whl/` path is purely
for the upstream PyTorch index (`/whl/cu128/torch/`, `/whl/cpu/torchvision/`,
etc.).

Two notable rewrites on this path:

- `error_page 403 =404` and `500 502 503 504 =404` — `download.pytorch.org`
  returns 403 for packages it doesn't carry (e.g. `/whl/cpu/six/`); uv treats
  403 as auth error and aborts resolution. Rewriting to 404 makes uv fall
  through to the default index. **Don't change this.**
- `proxy_redirect https://download.pytorch.org/ /` — clients can't follow
  HTTPS redirects from the proxy (cache-enforcer blocks the destination).

### `/packages/{2-hex}/...` — pythonhosted file downloads

Hash-based paths from rewritten pypi.org index responses. Proxied directly to
`https://files.pythonhosted.org/...` with long-term caching (`200 301 1M`).

The regex matches only hash-based paths (two hex characters after `/packages/`)
so flat paths like `/packages/{filename}.whl` (generated by pypiserver for
local wheelhouse packages) fall through to the generic wheel handler.

### `*.whl|*.tar.gz|*.zip` — wheel/tarball downloads

Proxies to local pypiserver, with `proxy_intercept_errors` falling through to
`pypi.org` on 404/5xx via `@pypi_fallback`. The fallback writes its access log
to `/data/logs/upstream/fallback.YYYY-MM-DD.log` on EFS — that's the
input that the wants-collector tails.

### Per-CUDA isolation: per-slug Service, not per-path

CUDA isolation is **not** done at the URL level. There is no
`/cu128/simple/...` route. Instead, each CUDA slug gets its own Deployment +
Service + EFS subdirectory (`/data/wheelhouse/{slug}/`), and runners are
configured at pod creation time with the URL of the right slug:

```
PIP_INDEX_URL=http://pypi-cache-cu128.pypi-cache.svc.cluster.local:8080/simple/
PIP_EXTRA_INDEX_URL=http://pypi-cache-cu128.pypi-cache.svc.cluster.local:8080/whl/cu128/
```

The `cuda_slug()` helper strips the patch version (`12.8.1` → `cu128`) to
match PyTorch's `download.pytorch.org/whl/cu128/` convention.

## CI Integration

There is **no composite GitHub Action** for index selection. Routing is baked
into the runner pod itself by the `arc-runners` module: each runner type is
generated per CUDA slug, and the runner ConfigMap sets these env vars on the
workflow container:

```
PIP_INDEX_URL          = http://pypi-cache-{slug}.pypi-cache.svc.cluster.local:8080/simple/
PIP_EXTRA_INDEX_URL    = http://pypi-cache-{slug}.pypi-cache.svc.cluster.local:8080/whl/{slug}/
PIP_TRUSTED_HOST       = pypi-cache-{slug}.pypi-cache.svc.cluster.local
UV_DEFAULT_INDEX       = http://pypi-cache-{slug}.pypi-cache.svc.cluster.local:8080/simple/
UV_INDEX               = http://pypi-cache-{slug}.pypi-cache.svc.cluster.local:8080/whl/{slug}/
UV_INSECURE_HOST       = pypi-cache-{slug}.pypi-cache.svc.cluster.local:8080
UV_INDEX_STRATEGY      = unsafe-best-match
PYPI_CACHE_SIMPLE_URL  = http://pypi-cache-{slug}.pypi-cache.svc.cluster.local:8080/simple/
PYPI_CACHE_WHL_URL     = http://pypi-cache-{slug}.pypi-cache.svc.cluster.local:8080/whl/{slug}/
```

CI authors don't need to do anything — pip/uv pick up these env vars
automatically. cache-enforcer prevents anyone from bypassing them.

The PYPI_CACHE_* variables are exposed for workflow scripts that build their own
URLs (e.g. for `--find-links`).

## Self-Learning Loop

End-to-end flow when a CI job triggers a source build:

1. Job runs `pip install foo-1.0.tar.gz` (or `pip install foo` and PyPI lacks a
   matching wheel). pip downloads the sdist via the proxy.
2. nginx serves the sdist from `pypi.org` via `@pypi_fallback`, recording the
   request in `/data/logs/upstream/fallback.YYYY-MM-DD.log` on EFS.
3. wants-collector tails the log on its next cycle, normalizes the package
   name (PEP 503), checks PyPI's JSON API for wheel coverage of the full
   `(python_versions × architectures × manylinux)` matrix, and if anything
   is missing writes the package to `wants/{cluster}.txt` in S3.
4. The external builder consumes `wants/*.txt` + `needbuild.txt`, builds the
   wheels, uploads them to `s3://pytorch-pypi-wheel-cache/{slug}/`.
5. wheel-syncer downloads new wheels to `/data/wheelhouse/{slug}/` on EFS via
   atomic rename. pypiserver's `cached-dir` backend picks them up on its next
   directory rescan.
6. Next time a job requests `foo` via `/simple/foo/`, the merge handler sees
   the local wheel, includes it (and prefers it on filename collision).

`needbuild.txt` is a manual override — packages listed there bypass both the
`prebuilt-cache.txt` check and the PyPI availability check, forcing them to the
wants list every cycle. This is how operators force-build packages that PyPI
*does* have wheels for (e.g. when those wheels are broken for our target
platform).

## Operational Gotchas

These are easy to break and have caused real outages.

### nginx cache key MUST include `$http_accept`

`proxy_cache_key "$request_uri|$http_accept"` (and `local:`/`upstream:` prefixes
for the internal merge subrequests). Removing `$http_accept` causes silent
collisions between PEP 503 (HTML) and PEP 691 (JSON) responses for the same URL,
serving HTML to JSON-expecting clients and vice versa. Hard to debug.

### sub_filter and njs URL rewriting are required, not cosmetic

nginx `sub_filter` rewrites `https://files.pythonhosted.org` and
`https://download.pytorch.org` to relative paths in upstream index responses.
`merge_indexes.js` does the same in subrequest results (sub_filter does not
apply to njs subrequest responses). Without this rewriting, pip would receive
absolute URLs that cache-enforcer blocks at the iptables level — every install
would fail.

### `proxy_redirect` rewrites also matter

`proxy_redirect https://pypi.org/ /` and
`proxy_redirect https://download.pytorch.org/ /` strip absolute hosts from
`Location` headers in 301/302 responses. Same reasoning as sub_filter — clients
can't reach the destination directly.

### `subrequest_output_buffer_size 100m` is load-bearing

njs subrequests buffer the full upstream response in memory before merging.
Default is 4k, which is far too small. Indexes for grpcio (~6 MB), aiohttp
(~7 MB), and the root pypi `/simple/` listing (~40 MB) all need the larger
buffer. nginx memory sizing (64 GiB on default `r5d.12xlarge` allocation) is
calibrated assuming this is set.

### `/whl/` 403→404 rewrite — uv compatibility

`error_page 403 =404 @pytorch_not_found;` and `error_page 500 502 503 504 =404`
on the PyTorch path. `download.pytorch.org` returns 403 for packages it
doesn't carry; uv treats 403 as auth error and aborts the entire resolution.
404 lets uv fall through to the default index. Don't change this.

### `prebuilt-cache.txt` header invalidation

The first line is `# matrix: py3.10,... x86_64,aarch64 manylinux_2_28`. If
`python_versions`, `target_architectures`, or `target_manylinux` change in
`clusters.yaml`, the header in S3 won't match what the collector expects, and
the entire prebuilt cache is invalidated (every package gets re-walked against
PyPI's JSON API). This is intentional — but it's a heavy operation and worth
doing during quiet hours.

### Adding a CUDA version is a fanout operation

A new entry in `pypi_cache.cuda_versions` produces a new Deployment, Service,
PDB, EFS subdirectory, and slot in the per-pod NVMe cache. Per-pod CPU/memory
shrinks (`pods_per_node = len(slugs)` in `compute_pod_resources`). Re-run the
deploy script after editing `clusters.yaml`.

### pypi-cache health is a hard runtime dependency

cache-enforcer blocks all direct egress to PyPI/pythonhosted/download.pytorch
on runner nodes. If the pypi-cache pods are unhealthy or the Service has no
endpoints, every `pip install` on every runner pod fails with a connection
error. Treat pypi-cache rollouts during business hours as risky.

### NetworkPolicy: ingress restricted to `arc-runners` namespace

`networkpolicy.yaml` allows ingress only from pods in namespace
`kubernetes.io/metadata.name: arc-runners`. Pods in other namespaces (e.g.
`buildkit`, `kube-system`) cannot reach pypi-cache directly.

### `docker/Dockerfile` is unused legacy

The `docker/` directory contains a Dockerfile that declares
`ENTRYPOINT ["python3", "/scripts/orchestrator.py"]` — but `orchestrator.py`
does not exist anywhere in the repo, and the deployed images are upstream
`pypiserver/pypiserver:v2.4.1` and `nginxinc/nginx-unprivileged:1.27-alpine`.
The Dockerfile is a vestige of an earlier architecture and is not built or
referenced by the deploy path.

## References

- `osdc-pypi-cache` skill — full operational reference (slug naming, NVMe
  sizing, IRSA roles, log rotation, NetworkPolicy, etc.)
- `modules/pypi-cache/` — module source (deploy script, manifests, scripts,
  terraform)
- `modules/cache-enforcer/` — iptables egress enforcement DaemonSet
- `clusters.yaml` — per-cluster `pypi_cache:` config block
