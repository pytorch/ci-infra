# Dev cluster (`lf-dev-aws-ue1`)

A cost-minimal OSDC cluster in the dev/test AWS account (us-east-1) for trying ideas against a test repo,
then tearing them down. It is not a pre-merge gate: staging stays that.

## What it runs

Only the job path:

| Kept | Why |
|------|-----|
| `eks` base: VPC (1 NAT gateway), EKS, Harbor | The cluster; Harbor serves our own images (`harbor:30002/osdc/*`) and node registry mirrors point at it |
| Base node DaemonSets (registry mirror, perf tuning, CVE mitigations, taint remover, NodeLocal DNS) | Nodes start with startup taints that only these remove |
| `karpenter` | Creates runner and workflow nodes |
| `arc` (controller, `runner-hooks-warmer`, wrapper) | The thing under test |
| `nodepools-dev` + `arc-runners-dev` | One runner type, `d-mt-l-x86iavx512-2-4` (max 4) |
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
| Base node disk (100 GB) | 8.00 | |
| 1 public IPv4 (NAT) | 3.65 | `nat_secondary_eip_count: 0` drops the 7 prod SNAT IPs |
| S3 / ECR / state, Harbor PVCs, KMS | ~5 | |

About $177/month before jobs. Each running job adds a runner node (m7g.xlarge, $0.16/h) and a workflow node
(c7a.2xlarge, $0.41/h); idle nodes are removed 10 s after their last pod. Example: a 30-minute job costs about $0.30,
and 80 runner-hours a month bring the total to $200–230.
Keep `eks_version` current: extended support raises the control plane to ~$438/month.

## Why staging costs more

Staging's floor is about $3,200/month, mostly base nodes and BuildKit warm nodes (~$1,316).

- Its 3 × m5.4xlarge base nodes reserve 24.5 vCPU / 87 GiB but use 0.37 vCPU / 5.45 GiB
  (meta-staging-aws-ue2, measured 2026-11-15).
- 49 idle listeners (one per runner type, 250m / 1331Mi each) hold half the CPU and three quarters of the memory
  reserved. Each uses about 12 MiB.
- The dev cluster has one runner type, so one listener, sized 100m / 256Mi.

## Why one t3a.large is enough

- Reservations come to about 1.6 vCPU / 3.6 GiB; a t3a.large has about 1.9 vCPU / 7 GiB usable. The Harbor and listener
  overrides are what make it fit: Harbor's chart defaults alone reserve 1.5 vCPU.
- It is burstable: it sustains 0.6 vCPU and the base node idles around 0.15 vCPU. Credit mode is AWS's default
  (unlimited), so a long burst is billed (~$0.05 per vCPU-hour above baseline), not throttled.
- Fallback if that ever costs more than it saves: m7i.large, $18.68/month more.

## Options we rejected

| Option | Saves/month | Why not |
|--------|-------------|---------|
| Karpenter and listeners on Fargate | nothing | Harbor still needs a node for its EBS volumes; each Fargate pod costs ~$9/month |
| Spot base node | ~$35 | One interruption stops listeners, Karpenter and Harbor together; jobs hang with no clear cause |
| Base node off at night | ~$30 | Jobs queued while it is off are missed; the node may come back in another AZ from Harbor's volumes |
| NAT instance instead of NAT gateway | ~$30 | Slow sustained bandwidth for image pulls; we would own patching it |
| Nodes in public subnets, no NAT | ~$33 | Every node gets a public IP; worth a look later |
| minikube / k3s on one machine | $73+ | Removes the EKS fee and is the long-term goal, but OSDC does not run there today |

## Risks

- **One base node is a single point of failure.** A replacement takes a few minutes; fine for dev.
- **The EKS version falls behind.** Extended support raises the control plane from $73 to ~$438/month. Upgrade dev with
  prod.
- **Dev drifts from prod.** Bugs in dropped modules show up only in staging, which stays the pre-merge check.

## Knobs this cluster uses

All default to the prod shape, so other clusters are unchanged.

| clusters.yaml key | Effect |
|-------------------|--------|
| `base.nat_secondary_eip_count` | Secondary EIPs per NAT gateway (default 7) |
| `base.cluster_log_types` | EKS control-plane log types to CloudWatch; `[]` disables (default all 5) |
| `base.base_node_subnet_count` | Pin the base node group to the first N subnets/AZs (default 0 = all) |
| `harbor.resource_requests.<nginx\|database>.<cpu\|memory>` | Harbor request overrides; passed to Helm only when set |
| `arc-runners.listener_cpu` / `arc-runners.listener_memory` | Listener requests = limits (default 250m / 1331Mi); GOMEMLIMIT = 90% of memory |

## First deploy (one time, in the test account)

Deployed manually, not from CI. Run the first deploy with the identity the team shares (e.g. the account's SSO admin
role): EKS makes it cluster admin, and that cannot change later. Grant anyone else access with
`aws eks create-access-entry`.

1. `just bootstrap lf-dev-aws-ue1` — state bucket `lf-osdc-tfstate-dev-ue1` and the lock table, in
   `STATE_REGION` (us-west-2) of this account.
2. `just deploy-base lf-dev-aws-ue1`, then `just deploy-module lf-dev-aws-ue1 karpenter` and `... arc` (`arc`
   creates the `arc-runners` namespace).
3. Create a GitHub App installed on `pytorch/pytorch-canary` (repository permissions: Administration read and write,
   Metadata read) and its secret `lf-dev-aws-ue1` in `arc-runners`, with keys `github_app_id`,
   `github_app_installation_id` and `github_app_private_key`.
4. `just deploy lf-dev-aws-ue1` for the remaining modules.
5. Add an AWS Budgets alert on the account (e.g. $300/month).

Tear down with `scripts/destroy-cluster.sh`.
