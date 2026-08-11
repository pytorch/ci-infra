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

- **Isolation:** `nodepools-agent-sandbox` fleet installs gVisor (runsc); the
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

## One-time prerequisite: build + push the agent image

Push to the in-cluster Harbor `osdc` project (nodes pull anonymously via the
`harbor:30002` mirror; `clusters.yaml` already points `agent_sandbox.agent_image`
at `harbor:30002/osdc/ci-agent-sandbox:prototype`):

```bash
docker buildx build --platform linux/amd64 -t ci-agent-sandbox:prototype \
  -o type=docker,dest=/tmp/agent.tar modules/agent-sandbox/agent

HARBOR_PASS=$(kubectl get secret harbor-admin-password -n harbor-system -o jsonpath='{.data.password}' | base64 -d)
kubectl port-forward --address 127.0.0.1 svc/harbor -n harbor-system 8081:80 >/dev/null 2>&1 &
crane auth login 127.0.0.1:8081 -u admin -p "$HARBOR_PASS"
crane push /tmp/agent.tar 127.0.0.1:8081/osdc/ci-agent-sandbox:prototype
```

(No secrets to create — the previous mitmproxy CA / GitHub token are gone.)

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
can't burst into another's CPU. **3 slots per `c7i.2xlarge`** fleet node:

| | vCPU | MiB |
|---|---|---|
| allocatable (8 vCPU / 16 GiB, maxPods 58) | 7.91 | 14710 |
| less fleet daemonsets (`alloy-logging` 0.51/1074, `hf-cache-mount` 0.11/672, rest ~0.30/396) | 0.92 | 2142 |
| free for sandboxes | 7.00 | 12568 |
| ÷ slot (2 vCPU / 4 GiB) | **3** | **3** |

Both numbers are measured on a live node, not derived from AWS-advertised specs —
`allocatable` already nets out kube-reserved, which scales with `maxPods`, so
don't scale this table linearly when changing instance size. The 2:4 slot matches
c7i's 1:2 vCPU:GiB ratio, so neither dimension strands capacity. The prototype
runs one replica, so today that's 1 of 3 slots.

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
- **gVisor install is best-effort** node userData (fail-closed if runsc doesn't register).
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
