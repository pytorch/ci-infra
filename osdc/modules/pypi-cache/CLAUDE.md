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
| `scripts/python/log_rotator.py` | Stdin-to-file log rotator with date-based rotation (mounted via ConfigMap) |
| `scripts/python/conftest.py` | pytest path setup for cross-module imports |
| `kubernetes/nginx.conf` | Static nginx caching proxy config (no template placeholders) |
| `kubernetes/nodepool.yaml.tpl` | Karpenter NodePool template (dedicated pypi-cache nodes) |
| `kubernetes/ec2nodeclass.yaml.tpl` | EC2NodeClass template (AWS node config for Karpenter) |
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
    log_max_age_days: 30
    cuda_versions:
      - "12.1"
      - "12.4"
    instance_type: r7i.12xlarge
    storage_request: "1Ti"
    server:
      cpu: "500m"
      memory: "768Mi"
    nginx:
      cpu: 2
      memory_gi: 2
      cache_size: "30Gi"
```

Per-cluster overrides work the usual way:

```yaml
clusters:
  arc-cbr-production:
    pypi_cache:
      cuda_versions:
        - "12.1"
        - "12.4"
    modules:
      - pypi-cache
```

## Dynamic pod sizing

When `instance_type` is set (default: `r7i.12xlarge`), pod resources are computed
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

Default sizing (r7i.12xlarge, 3 pods/node):
- Total per pod: 14 vCPU, 105 GiB
- nginx container: 2 vCPU, 2 GiB (fixed allocation)
- pypiserver container: 12 vCPU, 103 GiB (remainder)
- Pods: cpu, cu121, cu124

When `instance_type` is empty/unset, falls back to manual `server.*` config
values and runs on shared base infrastructure nodes.

## Architecture

```
Job pods set PIP_INDEX_URL per CUDA version:
  http://pypi-cache-cu121.pypi-cache.svc.cluster.local:8080/simple/

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
    cpu/           # access logs (access.YYYY-MM-DD.log)
    cu121/
    cu124/
```

## Request flow

### PyPI packages (default)

1. Composite GitHub Action sets `PIP_INDEX_URL` / `UV_INDEX_URL` for the job
2. pip/uv queries `pypi-cache-{slug}.pypi-cache.svc.cluster.local:8080/simple/{package}/`
3. nginx checks its local cache — serves response immediately on cache hit
4. On cache miss, nginx forwards to pypiserver on port 8081
5. pypiserver checks the corresponding wheelhouse — serves wheel if found
6. If not found, pypiserver returns 404 (or 500); nginx intercepts and proxies to
   `pypi.org` server-side (transparent fallback — no client-side redirect)
7. **nginx rewrites absolute `https://files.pythonhosted.org/packages/...` URLs in
   the response body to relative `/packages/...` paths** (`sub_filter`). This
   ensures pip fetches downloads through the proxy, not directly.
8. pip requests `/packages/...` from the proxy; nginx proxies to
   `files.pythonhosted.org` and caches the result (immutable packages cached 1 month)
9. nginx caches index responses (10m) and package downloads (1 month)
10. Access log records every request (the telemetry for self-learning)

### PyTorch packages (download.pytorch.org)

1. Composite GitHub Action sets `PIP_EXTRA_INDEX_URL` to
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

- **proxy_cache_lock**: Only one request per cache key hits pypiserver; concurrent
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
- **Cache storage**: 30 GiB emptyDir volume per pod (ephemeral, survives container
  restarts but not pod rescheduling)

## Self-learning loop

The builder is a future component. The current architecture captures access logs on
EFS so the builder can be added later without changing the serving infrastructure.

1. Job downloads `foo-1.0.tar.gz` via proxy (sdist = built from source)
2. Builder (future) sees `.tar.gz` in access logs
3. Builder runs `pip wheel foo==1.0` for each configured CUDA version
4. Next job gets pre-built wheel — instant install

## Log capture

pypiserver stdout is piped through `log_rotator.py` which provides dual logging:

- **stdout** — echoes every line to stdout for Kubernetes log collection (Alloy)
- **EFS files** — writes date-stamped files (`access.YYYY-MM-DD.log`) to `/data/logs/{slug}/`

The rotator automatically cleans up log files older than `log_max_age_days` (default 30).
Alloy log filtering (INFO suppression) is a follow-up task.

## Deploy ordering

1. Terraform creates EFS filesystem, mount targets, security group, CSI driver addon
2. `deploy.sh` reads terraform outputs (EFS filesystem ID)
3. Kustomize applies base resources (namespace, ServiceAccount)
4. ConfigMap created from `log_rotator.py` script
5. `generate_manifests.py` produces StorageClass, PVC, Deployments, Services, NodePools
6. Manifests applied in dependency order: StorageClass -> PVC -> **NodePools** -> Services -> Deployments
7. Waits for all Deployment rollouts

## Dependencies

- Base must be deployed (EKS cluster, VPC, subnets)
- Terraform creates EFS + CSI driver (runs before k8s manifests)
- Pods run on dedicated r7i.12xlarge nodes (Karpenter NodePool `pypi-cache`)
- When `instance_type` is unset, falls back to base infrastructure nodes (`CriticalAddonsOnly` taint)

## Portability boundary

`terraform/` is the ONLY cloud-specific code (AWS EFS, security groups, CSI driver).
All Kubernetes manifests are portable — the StorageClass is the sole integration point
between cloud storage and k8s workloads.
