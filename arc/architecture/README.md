# ci-infra architecture

The ci-infra infrastructure is managed via a combination of Terraform,
Makefiles scripts, and Python scripts for templating configuration files.

## High Level Architecture

This architecture provides redundant GitHub ARC Runner Scale Sets via the 3
EKS Cluster deployment configurations named "Prod", "Vanguard" and "Canary".
Allowing cluster deployments to be deployed in 3 separate stage deployments for
a complete rollout.

![High Level Architecture](./high-level-architecture.svg)

## EKS Architecture

![EKS Architecture](./eks-architecture.svg)

- EKS and the "green" node groups are managed via Terraform
- ARC Controller, Docker Mirror, and Karpenter are managed via Makefile scripts
- ARC Runner Scale Sets and Karpenter nodepools are managed via a combination
  of Makefile scripts and Python scripts that managed templating.
