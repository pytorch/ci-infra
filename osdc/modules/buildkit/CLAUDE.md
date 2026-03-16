# modules/buildkit/ — BuildKit Build Service

Dual-architecture container build service using `moby/buildkit`.

## What's here

| Path | Purpose |
|------|---------|
| `deploy.sh` | Reads config, runs generator, applies k8s resources, waits for rollout |
| `scripts/python/generate_buildkit.py` | Computes pod sizes from instance specs, generates Deployments + NodePools |
| `generated/` | Auto-generated Deployment + NodePool YAMLs (do not edit) |
| `kubernetes/base/` | Static resources: Namespace, Services, HAProxy LB, ConfigMap, NetworkPolicy |
| `node-setup.sh` | NVMe RAID0 setup (embedded as `text/x-shellscript` MIME part in NodePool userData) |

## Architecture

Two Deployments, two dedicated node types. Instance types and pod sizes are configurable via `clusters.yaml` and computed dynamically by the Python generator.

Default configuration:

| Arch | Instance | Service |
|------|----------|---------|
| arm64 | m8gd.24xlarge (Graviton3) | `buildkitd-arm64.buildkit:1234` |
| amd64 | m6id.24xlarge (Intel) | `buildkitd-amd64.buildkit:1234` |

Combined service `buildkitd.buildkit:1234` round-robins across both.

## Dynamic pod sizing

Pod resource requests are computed by `scripts/python/generate_buildkit.py` from a static instance spec table:

1. Look up total vCPU + memory for the instance type
2. Subtract kubelet reserved resources
3. Subtract DaemonSet overhead (300m CPU, 440Mi memory)
4. Apply 10% margin
5. Divide by pods_per_node (default: 2)

This ensures exactly N pods fit per node with Guaranteed QoS (requests == limits → static CPU pinning).

## Configuration (clusters.yaml)

```yaml
defaults:
  buildkit:
    replicas_per_arch: 4           # pods per architecture
    arm64_instance_type: m8gd.24xlarge
    amd64_instance_type: m6id.24xlarge
    pods_per_node: 2

clusters:
  my-cluster:
    buildkit:
      arm64_instance_type: m7gd.16xlarge  # override for regions without m8gd
```

## Adding a new instance type

Add an entry to `INSTANCE_SPECS` in `scripts/python/generate_buildkit.py` with vcpu, memory_gib, arch, nvme_count, nvme_size_gb.

## Key details

- `max-parallelism=1` — one build at a time per pod
- NVMe instance storage (RAID0) for build cache
- `buildkitd.toml` routes image pulls through Harbor
- NetworkPolicy restricts ingress to `arc-runners` namespace
- Pods use Guaranteed QoS with static CPU pinning
