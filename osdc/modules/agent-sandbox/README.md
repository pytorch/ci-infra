# agent-sandbox (PROTOTYPE)

A sandbox for running an untrusted AI agent in OSDC CI. The agent clones a
**public** repo, calls **Bedrock**, and returns a result — isolated under gVisor,
holding exactly one credential (a **read-only Bedrock IRSA role**) and nothing else.

It is **callable over the network like BuildKit**: a runner does
`curl sandbox-agent.ai-sandbox.svc:8080/run …`, gated by a NetworkPolicy that
allows the `arc-runners` namespace — **no RBAC on the caller**.

This is a **Phase-1 PoC**. There are **no credential proxies** and **no operator
secrets**: the agent clones public GitHub anonymously and calls Bedrock directly
with its scoped IRSA role.

## Architecture

```
[arc-runners] runner job ── curl ──► sandbox-agent.ai-sandbox.svc:8080/run
   (no RBAC; NetworkPolicy allow)              │
   ai-sandbox namespace                        ▼
   ┌───────────────────────────────────────────────────────────────────────┐
   │ [UNTRUSTED] sandbox-agent   Deployment, runtimeClassName: gvisor        │
   │   • holds ONE credential: a read-only Bedrock IRSA role (nothing else)  │
   │   • git clone (public repo, anonymous — no token)                       │
   │   • Bedrock InvokeModel via boto3 (signed with the pod's IRSA creds)    │
   │   • egress: OPEN internet (prototype; lockdown deferred)                │
   │        pinned to the ai-sandbox gVisor fleet                            │
   └───────────────────────────────────────────────────────────────────────┘
```

- **Isolation:** `nodepools-agent-sandbox` nodes boot from a custom AMI with
  gVisor (runsc) baked in; the
  `gvisor` RuntimeClass pins the worker there. IMDS hop-limit 1 keeps the node
  role out of reach — the agent gets only its own scoped IRSA role.
- **Credential:** a read-only Bedrock IRSA role (terraform) on the `sandbox-agent`
  SA. GitHub public repos are cloned anonymously (no token).
- **Invocation:** `sandbox-agent` Service (`:8080`), reachable from `arc-runners`
  via `sandbox-agent-ingress` NetworkPolicy — BuildKit parity.

## Endpoints

- `GET /healthz` → `{"status":"ok"}`
- `POST /run` body `{"repo","ref","task","model"?}` →
  `{"cloned":bool,"file_count":int,"top_level":[str],"report":str,"errors":{…}}`

  `top_level` is the clone's real top-level listing, which is also fed to the
  model — an empty one means the report was not grounded in the repo.

## The agent image

`deploy.sh` builds it and pushes it to the in-cluster Harbor `osdc` project
(public, so nodes pull anonymously via the `harbor:30002` mirror) — same pattern as
`modules/zombie-cleanup`. The tag is a content hash of `agent/`, so an unchanged
tree skips the build and a code change deploys a new immutable tag. Requires a
local docker daemon. No secrets to create.

## Deploy

```bash
just deploy-module meta-staging-aws-ue1 nodepools-agent-sandbox   # gVisor fleet
just deploy-module meta-staging-aws-ue1 agent-sandbox             # IRSA role + worker
```
`agent-sandbox` runs terraform (Bedrock IRSA role) → applies the namespace,
RuntimeClass, SA, worker + Service, NetworkPolicies → annotates the `sandbox-agent`
SA with the role.

## Use it (from anywhere with cluster network access, e.g. a runner)

```bash
curl -fsS -X POST http://sandbox-agent.ai-sandbox.svc.cluster.local:8080/run \
  -H 'Content-Type: application/json' \
  -d '{"repo":"pytorch/pytorch","ref":"main","task":"Summarize the build layout",
       "model":"<enabled-bedrock-model-or-inference-profile-id>"}'
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

Runs in the standard canary flow, gated by the `AGENT_SANDBOX` tag:
```
just integration-test meta-staging-aws-ue1
```

## Limitations (prototype)

- **Egress is OPEN** — the agent has internet access; egress lockdown
  (egress-restricted subnet / Security-Groups-for-Pods) is deferred, so network
  exfiltration is not yet mitigated.
- **The agent holds a credential** — a read-only Bedrock IRSA role. A prompt-injected
  agent could misuse it (bounded to Bedrock invoke cost/quota).
- **The gVisor AMI must be built per region** before the fleet can launch a node
  (`just build-agent-sandbox-ami <cluster>`), and it pins the AL2023 base — unlike
  the `al2023@latest` fleets it does not pick up CVE fixes on node rotation.
- **Warm worker, not per-task-ephemeral** (like buildkitd) — no per-task isolation reset yet.
- **Output is trusted as-is** — the propose/dispose validation gate is Phase 2.
- **Repo context is shallow** — the prompt carries the file count and the
  top-level listing, enough to keep answers grounded, but no file contents. Real
  tasks need reading files (and a tool loop to choose which); today the Bedrock
  call proves the credential path, not agent capability.

## Future direction: vetted MCP services

Secret-backed data sources (GitHub private, ClickHouse, Grafana, CloudWatch)
should be exposed to the agent as **vetted MCP servers** that hold the secrets and
expose only specific tools — the agent calls tools, never sees the secret. That
restores "the agent holds no data-source secrets" at a finer grain than a
credential proxy. Bedrock (the model backend) stays a direct call; MCP is for the
tools the model *uses*.
