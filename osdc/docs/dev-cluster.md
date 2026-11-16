# Dev cluster (`meta-dev-aws-ue1`)

A cost-minimal OSDC cluster in the dev AWS account (576436418915, us-east-1) for trying ideas against a test repo,
then tearing them down. It is not a pre-merge gate: staging stays that.

## What it runs

Only the job path:

| Kept | Why |
|------|-----|
| `eks` base: VPC (1 NAT gateway), EKS, Harbor | The cluster; Harbor serves our own images (`harbor:30002/osdc/*`) and node registry mirrors point at it |
| Base node DaemonSets (registry mirror, perf tuning, CVE mitigations, taint remover, NodeLocal DNS) | Nodes start with startup taints that only these remove |
| `karpenter` | Creates runner and workflow nodes |
| `arc` (controller, `runner-hooks-warmer`, wrapper) | The thing under test |
| `nodepools-dev` + `arc-runners-dev` | A small runner set: `d-mt-l-x86iavx512-2-4`, `d-mt-l-arm64g2-6-25` |
| `zombie-cleanup` | Stops a stuck runner pod from holding a node |

Dropped: `buildkit`, `keda`, `hf-cache`, `pypi-cache`, `cache-enforcer`, `agent-sandbox`, `monitoring`, `logging`,
`bin-pack-scheduler`, `harbor-cache-recovery`, the H100/B200 modules, warm-pool capacity and the node compactor.
Add a module back to `modules:` when an experiment needs it.

`nodepools-dev` replaces `nodepools`; never enable both on one cluster (fleet names collide, and
`runner_fleet_validator.py` rejects it). Its `c7i-runner` fleet keeps that exact name because `runner.yaml.tpl` and the
hooks warmer select `node-fleet=c7i-runner`.

## Cost (on-demand list prices, us-east-1)

| Always on | $/month | Why it cannot be off |
|-----------|---------|----------------------|
| EKS control plane | 73.00 | Every EKS cluster pays it |
| 1 × t3a.large base node | 54.90 | Listeners must stay connected to GitHub; Karpenter must run to create nodes; Harbor needs EBS PVCs (no Fargate) |
| NAT gateway | 32.85 | Pods are IPv6-only and GitHub is IPv4-only |
| Base node disk (50 GB) | 4.00 | |
| 1 public IPv4 (NAT) | 3.65 | `nat_secondary_eip_count: 0` drops the 7 prod SNAT IPs |
| S3 / ECR / state, Harbor PVCs, KMS | ~5 | |

About $173/month before jobs. Each running job adds a runner node (m7g.xlarge, $0.16/h) and a workflow node
(c7a.2xlarge $0.41/h or t4g.2xlarge $0.27/h); idle nodes are removed 10 s after their last pod.
Keep `eks_version` current: extended support raises the control plane to ~$438/month.

## Knobs this cluster uses

All default to the prod shape, so other clusters are unchanged.

| clusters.yaml key | Effect |
|-------------------|--------|
| `base.nat_secondary_eip_count` | Secondary EIPs per NAT gateway (default 7) |
| `base.cluster_log_types` | EKS control-plane log types to CloudWatch; `[]` disables (default all 5) |
| `base.base_node_disk_size` | Base node root volume, GiB (default 100) |
| `base.base_node_subnet_count` | Pin the base node group to the first N subnets/AZs (default 0 = all) |
| `harbor.resource_requests.<nginx\|database>.<cpu\|memory>` | Harbor request overrides; passed to Helm only when set |
| `arc-runners.listener_cpu` / `arc-runners.listener_memory` | Listener requests = limits (default 250m / 1331Mi); GOMEMLIMIT = 90% of memory |

## First deploy (one time, in account 576436418915)

1. Create the cluster admin IAM role named in `access_config.cluster_admin_role_names` (`osdc_dev_admin`).
2. `just bootstrap meta-dev-aws-ue1` — state bucket `ciforge-tfstate-meta-dev-aws-ue1` and the lock table, in
   `STATE_REGION` (us-west-2) of this account.
3. Create a GitHub App installed on `pytorch/pytorch-canary` and its secret `meta-dev-aws-ue1` in `arc-runners`
   (same shape as the staging secrets).
4. `just deploy meta-dev-aws-ue1`.
5. Add an AWS Budgets alert on the account (e.g. $300/month).

Tear down with `scripts/destroy-cluster.sh`.
