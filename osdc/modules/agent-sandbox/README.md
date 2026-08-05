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
  `{"cloned":bool,"file_count":int,"report":str,"errors":{…}}`

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
Omitting `model` still clones and returns `cloned: true` (Bedrock reports "no
model configured").

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
- **Bedrock model** — needs model access enabled + a valid invokable model/inference-profile ID for the region.

## Future direction: vetted MCP services

Secret-backed data sources (GitHub private, ClickHouse, Grafana, CloudWatch)
should be exposed to the agent as **vetted MCP servers** that hold the secrets and
expose only specific tools — the agent calls tools, never sees the secret. That
restores "the agent holds no data-source secrets" at a finer grain than a
credential proxy. Bedrock (the model backend) stays a direct call; MCP is for the
tools the model *uses*.
