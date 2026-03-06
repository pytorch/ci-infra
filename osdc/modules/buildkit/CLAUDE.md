# modules/buildkit/ — BuildKit Build Service

Dual-architecture container build service using `moby/buildkit`.

## What's here

| Path | Purpose |
|------|---------|
| `deploy.sh` | Expands NodePool templates, applies k8s resources, waits for rollout |
| `kubernetes/base/` | Namespace, Deployments (arm64 + amd64), Services, HAProxy LB, NetworkPolicy |
| `nodepool.yaml.tpl` | Karpenter NodePool template (expanded per arch by deploy.sh) |
| `node-setup.sh` | NVMe RAID0 setup, registry mirrors, CPU performance tuning |

## Architecture

Two Deployments, two dedicated node types:

| Arch | Instance | Service | Resources per pod |
|------|----------|---------|-------------------|
| arm64 | c7gd.16xlarge (Graviton3) | `buildkitd-arm64.buildkit:1234` | 30 vCPU, 60Gi |
| amd64 | m6id.24xlarge (Intel) | `buildkitd-amd64.buildkit:1234` | 47 vCPU, 188Gi |

Combined service `buildkitd.buildkit:1234` round-robins across both.

## Key details

- `max-parallelism=1` — one build at a time per pod
- NVMe instance storage (RAID0) for build cache
- `buildkitd.toml` routes image pulls through Harbor
- NetworkPolicy restricts ingress to `arc-runners` namespace
- Pods use Guaranteed QoS with static CPU pinning
