# Private ECR pull-through via Harbor

## Problem

The PyTorch CI images live in one registry, `308535385114.dkr.ecr.us-east-1.amazonaws.com`
(hardcoded as `ECR_REGISTRY` in `pytorch/pytorch`'s `docker-builds.yml`). ECR serves layer
downloads by redirecting to `prod-us-east-1-starport-layer-bucket.s3.us-east-1.amazonaws.com`
— an S3 host with no AAAA record, in **its own** region.

A cluster in us-east-1 matches that address against its local S3 prefix list and takes the
gateway endpoint. A cluster anywhere else does not: the prefix list only covers its own
region's S3, so the pull falls through to `0.0.0.0/0` and pays NAT plus inter-region
transfer, on every layer of every image.

## Design

Register the private ECR as a seventh Harbor proxy-cache upstream, alongside docker.io,
ghcr.io, public.ecr.aws, nvcr.io, registry.k8s.io and quay.io, and point containerd at it.

Nodes then pull from Harbor instead of ECR. The first pull of a layer fetches it
cross-region once and stores it in the cluster's own Harbor bucket; every later pull is
served from there over `s3.dualstack.<region>`, which resolves IPv6-first and egresses via
the egress-only gateway.

| Piece | Where |
|---|---|
| Harbor registry endpoint + `ecr-cache` proxy project | `scripts/python/configure_harbor_projects.py` |
| ECR read permission | `modules/eks/terraform/modules/harbor/main.tf` |
| containerd mirror | `base/kubernetes/registry-mirror-config.yaml` |
| Per-cluster gate `harbor.ecr_pullthrough` | `clusters.yaml`, wired in `justfile` |

## Constraints that shaped it

**The endpoint type must be `aws-ecr`, not `docker-registry`.** ECR tokens expire every 12
hours; only Harbor's ECR adapter performs the `GetAuthorizationToken` exchange and refreshes
them. A generic V2 endpoint would authenticate once and then rot.

**Credentials must be passed explicitly.** The proxy cache runs in `harbor-core`, which uses
the `default` ServiceAccount with no IRSA annotation, and base nodes set
`http_put_response_hop_limit = 1` so pods cannot reach IMDS. Core therefore has no ambient
AWS identity of any kind. The policy attaches to the existing `harbor_s3` IAM user — the
same no-IRSA workaround the S3 driver already documents in that file — and the key pair is
handed to Harbor at endpoint-creation time.

**The gate is the presence of a ConfigMap.** `registry-mirror-config` is applied by plain
kustomize with no per-cluster substitution, so the DaemonSet reads `registry-mirror-ecr`
with `optional: true`; absent means off. Two consequences:

- The DaemonSet reads it as an env var, so it only takes effect at pod start. The kustomize
  apply runs before Harbor deploys, so `_deploy-harbor` restarts the DaemonSet when the
  value changes.
- The script's marker-file check must include the conditional registry in its expected list,
  or a cluster that enables the flag later keeps skipping on the marker and never writes the
  mirror.

**The registry is always us-east-1**, regardless of the cluster's own region. That is where
the images are; pointing at a local ECR would find nothing.

## Scope

No effect on `meta-prod-aws-ue1` — its ECR pulls are already same-region and served by the
S3 gateway endpoint. This is for the off-region clusters: `ue2`, `uw1`, and `uw2` before it
takes traffic.

Enabling it on a cluster starts with a cold Harbor bucket, so the cache-miss path still
crosses regions once per layer while it warms.
