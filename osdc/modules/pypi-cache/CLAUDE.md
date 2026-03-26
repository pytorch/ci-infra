# modules/pypi-cache/ — Self-Learning PyPI Package Cache

Deployment-based PyPI cache on shared EFS storage. Runs one pypiserver Deployment
per CUDA version (cpu, cu121, cu124, etc.) with ClusterIP Services. Each pypiserver
instance serves a dedicated wheelhouse directory and captures access logs for a
future self-learning builder.

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
| `docker/Dockerfile` | Reserved for future builder image |

## Configuration (clusters.yaml)

```yaml
defaults:
  pypi_cache:
    namespace: pypi-cache
    server_port: 8080
    image: pypiserver/pypiserver:v2.4.1
    replicas: 2
    log_max_age_days: 30
    cuda_versions:
      - "12.1"
      - "12.4"
    storage_request: "1Ti"
    server:
      cpu_request: "100m"
      cpu_limit: "500m"
      memory_request: "256Mi"
      memory_limit: "512Mi"
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
    | pypiserver| | pypiserver| | pypiserver |
    | gunicorn  | | gunicorn  | | gunicorn   |
    +-----+-----+ +----+------+ +---+-------+
          |             |            |
          +-------------+------------+
                        |
          +-------------v------------+
          |  PVC: pypi-cache-data    |
          |  ReadWriteMany (EFS)     |
          +--------------------------+

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

1. Composite GitHub Action sets `PIP_INDEX_URL` / `UV_INDEX_URL` for the job
2. pip/uv queries `pypi-cache-{slug}.pypi-cache.svc.cluster.local:8080/simple/{package}/`
3. pypiserver checks the corresponding wheelhouse — serves wheel if found
4. If not found, pypiserver redirects to `pypi.org/simple/` (transparent fallback)
5. Access log records every request (the telemetry for self-learning)

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
5. `generate_manifests.py` produces StorageClass, PVC, Deployments, Services
6. Manifests applied in dependency order: StorageClass -> PVC -> Services -> Deployments
7. Waits for all Deployment rollouts

## Dependencies

- Base must be deployed (EKS cluster, VPC, subnets)
- Terraform creates EFS + CSI driver (runs before k8s manifests)
- Pods run on base infrastructure nodes (tolerate `CriticalAddonsOnly` taint)

## Portability boundary

`terraform/` is the ONLY cloud-specific code (AWS EFS, security groups, CSI driver).
All Kubernetes manifests are portable — the StorageClass is the sole integration point
between cloud storage and k8s workloads.
