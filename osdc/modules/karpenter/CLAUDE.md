# modules/karpenter/ — Karpenter Autoscaler

Deploys the Karpenter controller and its AWS infrastructure. This module handles the controller only — NodePools are managed by the `nodepools` module (or other modules that need custom compute).

## What's here

| Path | Purpose |
|------|---------|
| `terraform/` | IAM role (IRSA), SQS queue (spot interruptions), EventBridge rules, discovery tags |
| `helm/values.yaml` | Karpenter Helm chart base values (resources, tolerations, affinity, feature gates) |
| `deploy.sh` | Reads terraform outputs + clusters.yaml config, installs Helm chart |

## Terraform

Uses `terraform_remote_state` to read base outputs (OIDC provider, node instance role, security group, subnets). Creates:

- **IAM Role** — IRSA for the Karpenter service account with scoped EC2/SQS/SSM/IAM/EKS permissions
- **SQS Queue** — receives spot interruption, rebalance, and health events
- **EventBridge Rules** — routes EC2 and health events to the SQS queue
- **Discovery Tags** — `karpenter.sh/discovery` on cluster SG and private subnets

## Dependencies

- Base must be deployed (EKS cluster, OIDC provider, VPC/subnets must exist)
- Must be deployed before `nodepools`, `arc-runners`, or `buildkit`

## clusters.yaml config

```yaml
karpenter:
  replicas: 2           # Controller replica count
  log_level: info       # Controller log level
  pdb_enabled: true     # Pod disruption budget
  pdb_min_available: 1  # PDB minimum available
```
