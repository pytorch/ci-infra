# S3 Gateway VPC Endpoint

Why every OSDC VPC needs one, what it fixes, and what it does not cover.

## The problem

`NatGateway-Bytes` was the single largest line item on the OSDC AWS bill — larger
than all runner compute combined on a per-line basis. July 2026, grouped by the
`Cluster` cost-allocation tag:

| Cluster | Gross | Volume |
|---|---|---|
| `meta-prod-aws-ue2` | $126,855 | 2,819 TB |
| `meta-prod-aws-ue1` | $124,765 | 2,773 TB |
| `meta-prod-aws-uw1` | $1,269 | 26 TB |
| 3× `meta-staging-*` | $211 | 5 TB |
| *(account discount line, untagged)* | −$104,001 | — |
| **Net fleet total** | **$149,099** | 5,623 TB |

That is pure NAT gateway *data processing* at $0.045/GB — separate from the
$0.045/hr per-gateway rental, which is only ~$100/mo per cluster. The processing
fee was running 1,240× the rental fee.

## What was generating it

Almost all of it is **Harbor serving container image layers out of S3** — daily
NAT bytes track daily S3 GET count for `meta-prod-aws-ue1` at r² = 0.95, with no
meaningful non-S3 remainder, and it is overwhelmingly inbound (2,720 TB in vs
257 TB out).

Two design choices combined to route that through NAT:

1. **Harbor redirects layer pulls to S3.** `base/helm/harbor/values.yaml` sets
   `disableredirect: false`, so the registry 307-redirects containerd to a
   presigned `<bucket>.s3.<region>.amazonaws.com` URL and the node pulls the blob
   directly. This is deliberate and correct — it keeps multi-TB/day of bandwidth
   off the Harbor registry pods.
2. **No S3 gateway endpoint existed.** The OSDC VPC module was written from
   scratch and never had one. (The legacy ALI VPCs do — six S3 gateway endpoints
   in us-east-1 alone.) With no endpoint, S3 traffic matched the `0.0.0.0/0` NAT
   route.

Nodes are dual-stack, so this is not an IPv6 artifact: subnets carry both an
IPv4 CIDR and a `/64`, `EnableDns64` is `false`, and the standard S3 endpoint is
IPv4-only. S3 traffic leaves the ENI as IPv4 and hits the NAT route.

## The fix

`aws_vpc_endpoint.s3` in `modules/eks/terraform/modules/vpc/main.tf`, associated
with every private route table. Gateway endpoints have **no hourly charge and no
per-GB charge**, and in-region EC2→S3 transfer is free, so the endpoint installs
a more-specific prefix-list route and those bytes cost nothing.

Keep `disableredirect: false`. With the endpoint in place the redirect is
strictly optimal:

| Config | Layer bytes path | Rate |
|---|---|---|
| redirect on, no endpoint (old) | node → NAT GW → S3 | $0.045/GB |
| redirect off, no endpoint | node → Harbor pod (often cross-AZ) → NAT → S3 | $0.02/GB cross-AZ + $0.045/GB NAT |
| redirect on, endpoint (current) | node → gateway endpoint → S3 | **$0** |

## Terraform gotcha: inline routes vs endpoint routes

`aws_route_table.private` uses inline `route` blocks, which are authoritative —
Terraform normally deletes routes it did not declare. Gateway endpoint routes are
created by `ModifyVpcEndpoint`, not `CreateRoute`, so they *cannot* be declared
inline. The AWS provider skips `vpce-`-prefixed gateway routes when diffing route
tables, so the two resources coexist.

**Always run a second `tofu plan` after applying this to a new cluster and confirm
it is clean.** If a future provider version regresses that behaviour, the fix is
to split the private route tables into `aws_route_table` + separate `aws_route`
resources before re-applying.

## What it does not cover

- **Cross-region S3.** The prefix list is regional. PyTorch's public `ossci-*`
  buckets live in us-east-1, so a us-west-2 or us-west-1 cluster reading them
  still pays NAT plus cross-region transfer. Watch `NatGateway-Bytes` on
  `meta-prod-aws-uw2` specifically once it takes traffic — its residual will be
  structurally higher than ue1/ue2's.
- **Non-S3 internet egress.** pip, `docker.io` proxy-cache misses, GitHub. At
  ue1 this was inside the noise floor before the fix; it becomes the residual
  after. Re-measure rather than assuming it stays negligible.
- **The underlying volume.** ~100 TB/day of layer pulls is still happening, it is
  just free now. With ~6,000 Karpenter node launches/day and a ~500 GiB
  node-local image cache (`base/kubernetes/image-cache-janitor/`), every cold
  node pulls ~16 GB from scratch. Reducing that is a separate workstream (lazy
  loading / SOCI, AMI pre-bake, longer node TTL).

## Verification

```bash
CLUSTER=meta-staging-aws-ue1
REGION=$(uv run scripts/cluster-config.py "$CLUSTER" region)
VPC=$(aws eks describe-cluster --name "$CLUSTER" --region "$REGION" \
        --query 'cluster.resourcesVpcConfig.vpcId' --output text)

# Endpoint exists and is available
aws ec2 describe-vpc-endpoints --region "$REGION" \
  --filters "Name=vpc-id,Values=$VPC" "Name=service-name,Values=com.amazonaws.$REGION.s3" \
  --query 'VpcEndpoints[].{id:VpcEndpointId,type:VpcEndpointType,state:State,rtbs:RouteTableIds}'

# Prefix-list route present on each private route table
aws ec2 describe-route-tables --region "$REGION" \
  --filters "Name=vpc-id,Values=$VPC" "Name=tag:Name,Values=*-private-*" \
  --query 'RouteTables[].{rtb:RouteTableId,vpce:Routes[?starts_with(GatewayId || ``, `vpce-`)].GatewayId}'
```

A single private route table is expected where `single_nat_gateway: true` (the
staging clusters); prod gets one per AZ. What matters is that every private
subnet maps to a table carrying the route.

`modules/eks/tests/smoke/test_eks.py::TestS3GatewayEndpoint` asserts gateway
type, `available` state, association with every private route table, and — the
load-bearing one — that each of those tables actually holds an `active`
prefix-list route to the endpoint. The association alone is not enough: it can
survive the route being pruned, which is exactly the inline-route failure mode
above. `just smoke <cluster>` covers all four going forward.

### Behavioural check

`AWS/NATGateway` → `BytesInFromDestination` should collapse by >90% over a normal
traffic period. On an idle cluster there is nothing to see — `meta-staging-aws-ue1`
sits at ~0.2 GB/hr — so drive traffic deliberately instead:

```bash
just kubeconfig "$CLUSTER"

# ~3.4 GB of in-region S3. ossci-linux is public and us-east-1, so it is inside
# the same prefix list and needs no credentials. Base nodes are in the private
# subnets; the toleration is what schedules the pod onto one.
kubectl run s3-endpoint-check --rm -i --restart=Never \
  --image=public.ecr.aws/docker/library/alpine:3.21 \
  --overrides='{"spec":{"tolerations":[{"key":"CriticalAddonsOnly","operator":"Exists","effect":"NoSchedule"}]}}' \
  -- sh -c 'for i in 1 2 3; do wget -q -O /dev/null \
      https://ossci-linux.s3.us-east-1.amazonaws.com/cuda/cuda_7.0.28_linux.run; done'
```

Then re-read the metric after ~10 minutes (NAT stats publish on a lag). The S3
pull should be invisible against the idle floor.

**Run a negative control in the same window** — a broken measurement is
indistinguishable from success otherwise. Repeat the pod against a non-AWS host
(e.g. a ~140 MB `cdn.kernel.org` tarball); those bytes *must* appear on the NAT
gateway. Only the two-sided result proves anything.

For per-request proof, CloudTrail S3 data events are enabled account-wide and
carry `vpcEndpointId`. That is unambiguous, but reading them means Athena over
the trail bucket plus delivery lag — a one-off pre-prod check, not something to
automate.

## Rollback

Delete the endpoint. Traffic falls back to the `0.0.0.0/0` NAT route immediately;
there is no data-plane state to drain.

## Risks reviewed before rollout

- **Source-IP allowlists** are the usual way a gateway endpoint breaks things —
  the source becomes a private IP plus an `aws:SourceVpce` condition key rather
  than a NAT EIP. Checked: `<cluster>-harbor-registry` and
  `pytorch-hf-model-cache-<cluster>` have no bucket policy at all.
- **Endpoint policy** is left at the default full-access. A restrictive policy
  here would silently break job-level S3 reads.
- **Route table limits**: private tables carry 4 routes; the cap is 50.
