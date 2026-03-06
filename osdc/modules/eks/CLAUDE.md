# modules/eks/ — AWS EKS Cluster Infrastructure

All AWS infrastructure: VPC, EKS cluster, managed node group, Harbor S3/IAM. This module creates the foundation that all other modules build on.

## What's here

| Path | Purpose |
|------|---------|
| `terraform/` | VPC, EKS, Harbor S3/IAM (single parameterized root, no per-env dirs) |
| `terraform/modules/vpc/` | VPC, subnets, NAT gateways |
| `terraform/modules/eks/` | EKS cluster, managed node group, OIDC provider, IAM |
| `terraform/modules/harbor/` | S3 bucket + IAM for Harbor registry storage |
| `images.yaml` | Harbor bootstrap images to mirror to ECR |
| `scripts/mirror-images.sh` | Copies bootstrap images from upstream registries to ECR |

## Terraform state

Uses `<cluster-id>/base/terraform.tfstate` as the state key (historical, predates the module extraction).

## Dependencies

None — this is the foundational module. All other modules depend on its outputs.

## Who reads our outputs

- `deploy-base` (justfile) — reads Harbor outputs for Helm install
- `modules/karpenter/` — reads OIDC provider, node role, SG, subnets via `terraform_remote_state`
- `modules/karpenter/deploy.sh` — reads `cluster_endpoint` via `tofu output`
