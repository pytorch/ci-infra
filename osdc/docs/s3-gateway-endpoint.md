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
| `pytorch-re-prod-production` + 3× staging | $368 | 8 TB |
| *(account discount line, untagged)* | −$104,001 | — |
| **Net fleet total** | **$149,257** | 5,631 TB |

That is pure NAT gateway *data processing* at $0.045/GB — separate from the
$0.045/hr per-gateway rental, which is only ~$100/mo per cluster. The processing
fee was running 1,240× the rental fee.

## What was generating it

Regressing daily `NatGateway-Bytes` against daily S3 GET count
(`Requests-Tier2`) for `meta-prod-aws-ue1`, 7–31 July 2026:

```
n = 25 days
Pearson r  = 0.9729      r² = 0.9465
slope      = 64.3 MB per S3 GET
intercept  = −6,186 GB/day   (statistically zero)
```

An intercept indistinguishable from zero means there is effectively no non-S3
traffic on the NAT gateways. 64.3 MB/GET is the size profile of a container
image layer blob. Direction confirms it: 2,720 TB inbound vs 257 TB outbound.

Two design choices combined to route all of it through NAT:

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
CLUSTER=meta-prod-aws-ue1
REGION=$(yq ".clusters.$CLUSTER.region" clusters.yaml)
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

`modules/eks/tests/smoke/test_eks.py::TestS3GatewayEndpoint` asserts all three
invariants (gateway type, available, associated with every private route table),
so `just smoke-test <cluster>` covers this going forward.

The behavioural check is `AWS/NATGateway` → `BytesInFromDestination`, which should
collapse by >90% within an hour of a normal traffic period.

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
