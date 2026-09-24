# Public endpoint (proposal, not deployed)

Today `/run` is a ClusterIP that `sandbox-agent-ingress` opens to the `arc-runners`
namespace only, so a caller must run on an OSDC self-hosted runner. The framework RFC has
any consumer workflow call the dispatcher with its OIDC token, GitHub-hosted runners
included. This page lists what that needs. Nothing here is deployed.

## What already holds

Every call carries a verified GitHub OIDC token (`REQUIRE_AUTH=true`; its rollback setting
is one of the gaps below), is matched against
a capability manifest pinned on the default branch, and gets a Grant that bounds what the
run may do. The client is untrusted by design. A public endpoint changes who can *reach*
the dispatcher, not who is *admitted*.

The dispatcher side is ready: a manifest opts in to GitHub-hosted callers with
`clients.runner_environments: [self-hosted, github-hosted]`. The default stays
`[self-hosted]`, and a unit test keeps every checked-in manifest reachable today.

## What it needs

1. **A load balancer controller.** The clusters run IPv6 VPC-CNI, so the load balancer
   must target pod IPs over IPv6. That needs the AWS Load Balancer Controller (IP targets,
   dualstack), which OSDC does not install today: a new module with its own IRSA role.
2. **An internet-facing ALB** in front of the `sandbox-agent` Service, from an Ingress.
   ALB rather than NLB because AWS WAF attaches only to an ALB, and WAF is the cheapest
   place for a rate limit. Raise the ALB idle timeout above the client's `RUN_TIMEOUT_S`
   (1080 s in `action/sandbox_client.py`), e.g. `idle_timeout.timeout_seconds=1200` in the
   Ingress's load-balancer attributes: a waiting `/run` sends nothing until the task ends,
   and at the 60 s default the ALB would answer 504 while the task keeps its slot, leaving
   the caller without the task id it needs to fetch the result. Point the target group's
   health check at `/healthz`.
3. **TLS and DNS.** An ACM certificate and a Route53 alias for a hostname under a zone
   the PyTorch infra account owns. HTTPS only; no port-80 listener.
4. **A NetworkPolicy rule** admitting the ALB. With IP targets the ALB connects to the
   pods from its own subnets, so `sandbox-agent-ingress` needs an `ipBlock` for those
   subnet CIDRs on port 8080, next to the existing `arc-runners` rule.
5. **A rate-based WAF rule.** Per-IP limits are coarse because GitHub-hosted runners
   share addresses; they stop a flood, not a noisy tenant. Body size stays enforced by
   the dispatcher (`MAX_BODY_BYTES`, 64 KiB): WAF on an ALB inspects only the first
   8 KiB of a body, so it cannot enforce that limit.
6. **Opt in per manifest.** Add `github-hosted` to the manifests that should accept it,
   and point callers at the new URL through the action's `endpoint` input.

## Gaps to close before exposing it

- **Per-caller quotas.** Each dispatcher replica caps concurrency (`MAX_CONCURRENT_TASKS`)
  but there is no per-manifest or per-repository budget, so one admitted caller can hold
  every slot. This exists today; a public endpoint makes it cheaper to trigger, since
  a caller on GitHub-hosted runners is not bounded by OSDC runner capacity.
- **Token replay.** `jti` is not consumed, so a leaked token is usable until it expires.
  Consuming it needs state shared by both dispatcher replicas.
- **The `REQUIRE_AUTH=false` rollback.** With the flag off, a request with no
  `Authorization` header gets the v1 Grant without any token check (`_grant_for` in
  `dispatcher/http_api.py`). Today that reopens `/run` to `arc-runners` only; behind this
  endpoint it would admit anonymous callers from the internet. Remove the unauthenticated
  path, or refuse to start with it while the endpoint exists, before going public.
- **`/healthz` exposes capacity.** The ALB health check needs it, so keep it off the public
  listener with a listener rule that answers 404 for that path, rather than hiding it
  behind the internal Service.
- **Security review.** A new internet-facing service in the PyTorch infra account needs
  one before it goes live.

## Decisions

- The hostname and the zone it lives in.
- Who owns the load balancer controller module (OSDC).
- Whether GitHub-hosted callers are needed for the first launch, or pr-review ships on
  the OSDC runners first and this follows.
