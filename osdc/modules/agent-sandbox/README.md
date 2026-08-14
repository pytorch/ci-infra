# agent-sandbox (PROTOTYPE)

A sandbox for running an untrusted AI agent in OSDC CI. The agent can *read* from
GitHub and AWS (incl. **Bedrock**) and return results — without ever holding a
credential, and running under gVisor on a dedicated node fleet.

It is **callable over the network like BuildKit**: a runner does
`curl sandbox-agent.ai-sandbox.svc:8080/run …`, gated by a NetworkPolicy that
allows the `arc-runners` namespace (no K8s RBAC on the caller) — exactly how a
runner reaches `buildkitd` on `:1234`.

This is a **Phase-1 PoC**: it proves the security spine (gVisor isolation +
"use secrets without holding them" + git-clone/Bedrock through proxies +
BuildKit-style network invocation). The propose/dispose output gate, VPC-layer
egress lockdown, and a per-task-ephemeral (vs warm) worker are deferred (see
*Limitations*).

## Architecture

```
[arc-runners] runner job ── curl ──► sandbox-agent.ai-sandbox.svc:8080/run
   (no RBAC; NetworkPolicy allow)              │
                                               ▼
   ai-sandbox namespace
   ┌───────────────────────────────────────────────────────────────────────┐
   │ [UNTRUSTED] sandbox-agent   Deployment, runtimeClassName: gvisor        │
   │   • long-running HTTP worker, no credentials, no K8s token              │
   │   • http ─────────► sigv4-proxy (signs Bedrock/AWS with IRSA)           │
   │        pinned to the ai-sandbox gVisor fleet                            │
   └───────────────────────────────────────────────────────────────────────┘
     the credential never shares a node / gVisor sandbox with agent code.
```

- **Isolation:** `nodepools-agent-sandbox` nodes boot from a custom AMI with
  gVisor (runsc) baked in; the `gvisor` RuntimeClass pins the worker there. IMDS
  hop-limit 1 keeps the node role out of reach.
- **Credential:** held by the proxy, never by the worker. `aws-sigv4-proxy` signs
  AWS requests with a read-only IRSA role (terraform); the worker sends unsigned
  HTTP. Public repos are cloned directly, so there is no GitHub credential at all.
- **Invocation:** `sandbox-agent` Service (`:8080`), reachable from `arc-runners`
  via `sandbox-agent-ingress` NetworkPolicy — BuildKit parity.

## Endpoints

- `GET /healthz` → `{"status":"ok"}`
- `POST /run` body `{"repo","ref","task","model"?}` →
  `{"cloned":bool,"file_count":int,"top_level":[str],"report":str,"errors":{…}}`

  `top_level` is the clone's real top-level listing, which is also fed to the
  model — an empty one means the report was not grounded in the repo.

## The agent image

Built and pushed by hand to the in-cluster Harbor `osdc` project (public, so nodes
pull it anonymously via the `harbor:30002` mirror), and referenced from
`clusters.yaml` → `agent_sandbox.agent_image`. No secrets to create.

```bash
docker buildx build --platform linux/amd64 -t ci-agent-sandbox:prototype \
  -o type=docker,dest=/tmp/agent.tar modules/agent-sandbox/agent

HARBOR_PASS=$(kubectl get secret harbor-admin-password -n harbor-system -o jsonpath='{.data.password}' | base64 -d)
kubectl port-forward --address 127.0.0.1 svc/harbor -n harbor-system 8081:80 >/dev/null 2>&1 &
crane auth login 127.0.0.1:8081 -u admin -p "$HARBOR_PASS"
crane push /tmp/agent.tar 127.0.0.1:8081/osdc/ci-agent-sandbox:prototype
```

## Deploy

```
just deploy-module meta-staging-aws-ue1 nodepools-agent-sandbox   # gVisor fleet
just deploy-module meta-staging-aws-ue1 agent-sandbox             # IRSA + proxies + worker
```

## Use it (from anywhere with cluster network access, e.g. a runner)

```
curl -fsS -X POST http://sandbox-agent.ai-sandbox.svc.cluster.local:8080/run \
  -H 'Content-Type: application/json' \
  -d '{"repo":"pytorch/pytorch","ref":"main","task":"Summarize the build layout",
       "model":"anthropic.claude-3-5-sonnet-20240620-v1:0"}'
```
Any caller can pick the model per request. Omitting `model` falls back to
`BEDROCK_DEFAULT_MODEL_ID`, set at deploy time from `clusters.yaml` →
`agent_sandbox.default_model_id`.

## Capacity

A sandbox slot is **2 vCPU / 4 GiB with requests == limits** (Guaranteed QoS), so
capacity per node is a division rather than a guess — and one untrusted sandbox
can't burst into another's CPU. **3 slots per `c7a.2xlarge`** fleet node:

| | vCPU | MiB |
|---|---|---|
| allocatable (8 vCPU / 16 GiB, maxPods 58) | 7.91 | 14624 |
| less fleet daemonsets (`alloy-logging` 0.51/1074, `hf-cache-mount` 0.11/672, rest ~0.30/396) | 0.92 | 2142 |
| free for sandboxes | 7.00 | 12482 |
| ÷ slot (2 vCPU / 4 GiB) | **3** | **3** |

Both numbers are measured on a live node — AWS-advertised specs overstate usable
memory, and `allocatable` already nets out kube-reserved, which scales with
`maxPods`. Don't scale this table linearly when changing instance size.

Memory is the tighter dimension: ~194 MiB spare beyond the 3rd slot, so a
cluster-wide daemonset gaining ~200 MiB of requests silently costs a slot. The
prototype runs one replica, so today that's 1 of 3 slots.

## Choosing the Bedrock model

**It must be a cross-region inference profile ID (`us.` / `global.` prefix), not
a bare foundation-model ID.** In `us-east-1` every Anthropic model on Bedrock is
`INFERENCE_PROFILE`-only; the one still advertising `ON_DEMAND`
(`anthropic.claude-3-haiku-20240307-v1:0`) is refused by the provider:

```
ResourceNotFoundException: Access denied. This Model is marked by provider as
Legacy and you have not been actively using the model in the last 30 days.
```

So "just use an old cheap model" is not an option — the current default is
`us.anthropic.claude-haiku-4-5-20251001-v1:0` (cheapest active model). To list
what this account can actually invoke:

```bash
aws bedrock list-foundation-models --region us-east-1 --by-provider anthropic \
  --query 'modelSummaries[].[modelId,inferenceTypesSupported,modelLifecycle.status]' --output table
aws bedrock list-inference-profiles --region us-east-1 \
  --query 'inferenceProfileSummaries[?contains(inferenceProfileId, `anthropic`)].inferenceProfileId' --output table
```

Because a `us.` profile routes the request to any US region, the IRSA policy
grants `bedrock:InvokeModel` on `arn:aws:bedrock:*::foundation-model/*` in
addition to this region's inference profiles — with the region pinned, invokes
fail `AccessDenied` whenever routing leaves the cluster's region.

## Integration test

Runs as part of the standard canary flow, gated by the `AGENT_SANDBOX` tag
(requires the `agent-sandbox` module). After deploying to staging:
```
just integration-test meta-staging-aws-ue1
```
The `test-agent-sandbox` job runs on a normal runner and `curl`s the sandbox
Service — asserting it is reachable from `arc-runners` (BuildKit parity) and that
it clones a repo through the proxy without the runner or worker holding a token.

## Limitations (prototype — read before trusting it)

- **Egress is NOT hard-enforced.** Under IPv6-only AWS VPC-CNI, `NetworkPolicy`
  doesn't cover IPv4 egress (the same gap that made cache-enforcer's node iptables
  unreliable), so the proxies are the *credential* boundary, not a network one: a
  compromised agent still has no token to steal, but can still reach the internet.
  The real boundary — a dedicated sandbox subnet with no NAT/IGW route +
  Security-Groups-for-Pods — is Phase 2.
- **The gVisor AMI must be built per region** before the fleet can launch a node
  (`just build-agent-sandbox-ami <cluster>`), and it pins the AL2023 base — unlike
  the `al2023@latest` fleets it does not pick up CVE fixes on node rotation.
- **Warm worker, not per-task-ephemeral.** One long-running worker handles tasks
  sequentially (like buildkitd), so there is no per-task isolation reset yet.
  Per-task-ephemeral pods (a dispatcher that creates a Job per request) are Phase 2.
- **Output is trusted as-is.** The propose/dispose validation + approval gate is
  Phase 2.
  `aws-sigv4-proxy:latest`) — digest-pin before non-prototype use.
- **Repo context is shallow** — the prompt carries the file count and the
  top-level listing, enough to keep answers grounded, but no file contents. Real
  tasks need reading files (and a tool loop to choose which); today the Bedrock
  call proves the credential path, not agent capability.

## Future direction: vetted MCP services

Secret-backed data sources (ClickHouse, Grafana, CloudWatch) should be exposed as
**vetted MCP servers** that hold the secrets and expose only specific tools — the
same principle as the proxies, at a finer grain than a whole-host allowlist.
