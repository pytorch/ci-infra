# modules/cache-enforcer/ — Registry and PyPI Cache Enforcement

DaemonSet that installs iptables rules on runner nodes to block direct outbound
access to external container registries and PyPI. Forces all traffic through
internal caches already running on each node.

## What's here

| Path | Purpose |
|------|---------|
| `kubernetes/kustomization.yaml` | Kustomize entry point |
| `kubernetes/configmap.yaml` | Shell script that installs iptables rules |
| `kubernetes/daemonset.yaml` | DaemonSet + ServiceAccount |

## Enforcement rules

| Traffic | Blocked destinations | Forced through |
|---------|---------------------|----------------|
| Container pulls | docker.io, registry-1.docker.io, auth.docker.io, production.cloudflare.docker.com, ghcr.io, nvcr.io, quay.io, registry.k8s.io | Harbor (localhost:30002) |
| Python packages | pypi.org, files.pythonhosted.org | pypi-cache (localhost:8080) |

`public.ecr.aws` is NOT blocked — no rate limits and not proxied through Harbor.

## Mechanism

Uses `xt_string` kernel module (standard on AL2023) to match domain names in:
- TLS ClientHello SNI field (port 443)
- HTTP Host header (port 80)

Rules inserted into a `CACHE_ENFORCER` chain, jumped from both `OUTPUT`
(host processes like containerd) and `FORWARD` (pod traffic via VPC CNI).

REJECT with tcp-reset gives fast `Connection refused` instead of timeout.
IPv4 and IPv6 rules applied (ip6tables skipped if unavailable).

## Idempotency

The `CACHE_ENFORCER` chain is flushed and rebuilt on every pod restart.
Jump rules from OUTPUT/FORWARD are checked before inserting. Safe across
reboots and DaemonSet rollouts.

## Deployment ordering

Runs before runner job pods schedule — the `git-cache-not-ready` startup
taint blocks job pods until DaemonSets finish. The iptables init container
completes in sub-seconds.

## Dependencies

- **Harbor** — must be running (base deploy). Container image pulls route
  through Harbor at localhost:30002 (NodePort).
- **pypi-cache** — must be deployed for PyPI blocking to work. Without it,
  pip installs fail entirely. Deploy pypi-cache before or alongside this module.

## Adding or removing domains

Edit `REGISTRY_DOMAINS` or `PYPI_DOMAINS` in `kubernetes/configmap.yaml`,
then redeploy:

```bash
just deploy-module <cluster> cache-enforcer
```

The rolling update restarts pods, re-running the init container with new rules.

## Limitation: ECH

TLS Encrypted Client Hello encrypts the SNI field, bypassing string matching.
None of the blocked domains currently use ECH. If they adopt it, migrate to
Cilium CNI with `toFQDNs` DNS-based policies.
