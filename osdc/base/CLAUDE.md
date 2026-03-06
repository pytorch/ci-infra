# base/ — Shared Cluster Infrastructure

Everything here is deployed to **every** cluster. If it's optional, it belongs in `modules/` instead.

## What's here

| Directory | Contents |
|-----------|----------|
| `kubernetes/` | Base k8s resources: gp3 StorageClass, NVIDIA device plugin, git-cache-warmer DaemonSet, Harbor namespace, node performance tuning |
| `helm/harbor/` | Harbor Helm values. Component images overridden via `--set` (no `global.imageRegistry` in chart). |
| `docker/runner-base/` | Base container image for runner pods |
| `scripts/bootstrap/` | EKS node bootstrap (registry mirrors, sysctl tuning) |

## Key constraints

- All base nodes are tainted `CriticalAddonsOnly=true:NoSchedule`. Any workload here must tolerate it.
- Harbor chart has per-component image overrides, not global. Check `helm/harbor/values.yaml` comments.
- Harbor bootstrap images (`images.yaml`) have moved to `modules/eks/`. Don't add them here.
