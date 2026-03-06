# modules/arc/ — Actions Runner Controller

Installs the ARC controller Helm chart. This module provides the **controller** — the actual runner scale sets are deployed by the `arc-runners` module.

## What's here

| Path | Purpose |
|------|---------|
| `deploy.sh` | `helm upgrade --install` for ARC controller chart |
| `kubernetes/` | Namespaces (`arc-systems`, `arc-runners`), runner ServiceAccount, LimitRange, hooks ConfigMap, registry mirror config |
| `helm/arc/values.yaml` | ARC controller Helm values |

## Dependencies

- Base must be deployed (Karpenter, Harbor running)
- No terraform — ARC is pure k8s/helm

## Key constraint

Runner pods run with `ACTIONS_RUNNER_REQUIRE_JOB_CONTAINER=true`. Every workflow job **must** specify a `container:` image.
