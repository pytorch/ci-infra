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
   │   • HTTPS_PROXY ─► agent-vault  (injects read-only GitHub token on wire)│
   │   • http ─────────► sigv4-proxy (signs Bedrock/AWS with IRSA)           │
   │        pinned to the ai-sandbox gVisor fleet                            │
   └───────────────────────────────────────────────────────────────────────┘
     agent-vault + sigv4-proxy run on base nodes (OFF the sandbox fleet):
     the credential never shares a node / gVisor sandbox with agent code.
```

- **Isolation:** `nodepools-agent-sandbox` fleet installs gVisor (runsc) via node
  userData; the `gvisor` RuntimeClass pins the worker there.
- **Credentials:** `agent-vault` — a **mitmproxy**-based header-injection proxy
  (a ~10-line addon that default-denies non-allow-listed hosts and injects the
  GitHub token on the wire) for GitHub; `aws-sigv4-proxy` for AWS/Bedrock via a
  read-only IRSA role (terraform). The worker holds neither. (We initially picked
  Infisical Agent Vault, but it is a stateful server — accounts/vaults/DB — not a
  config-file injector, so the prototype uses mitmproxy, which we fully own.)
- **Invocation:** `sandbox-agent` Service (`:8080`), reachable from `arc-runners`
  via `sandbox-agent-ingress` NetworkPolicy — BuildKit parity.

## Endpoints

- `GET /healthz` → `{"status":"ok"}`
- `POST /run` body `{"repo","ref","task","model"?}` →
  `{"cloned":bool,"file_count":int,"report":str,"errors":{…}}`

## One-time prerequisites (operator, out-of-band)

1. **Build + push the agent image**, then set `agent_sandbox.agent_image` in
   `clusters.yaml`:
   ```
   docker build -t <registry>/ci-agent-sandbox:prototype modules/agent-sandbox/agent
   docker push  <registry>/ci-agent-sandbox:prototype
   ```
2. **Create the two Secrets** in `ai-sandbox` (NOT committed):
   - `agent-sandbox-creds` — the injected credential (read-only!):
     ```
     kubectl create secret generic agent-sandbox-creds -n ai-sandbox \
       --from-literal=github-token=<READ-ONLY, single-repo GitHub App token>
     ```
   - `agent-vault-ca` — the MITM CA the worker trusts (generate once):
     ```
     openssl req -x509 -newkey rsa:2048 -nodes -days 365 \
       -keyout ca.key -out ca.crt -subj "/CN=agent-vault-ca"
     kubectl create secret generic agent-vault-ca -n ai-sandbox \
       --from-file=ca.crt=ca.crt --from-file=ca.key=ca.key
     ```
3. **Pin the proxy images by digest** (`mitmproxy/mitmproxy`, `aws-sigv4-proxy`)
   before any non-prototype use.

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
  doesn't cover IPv4 egress (the same gap that made cache-enforcer's node
  iptables unreliable). The real boundary — a dedicated sandbox subnet with no
  NAT/IGW route + Security-Groups-for-Pods — is **Phase 2**, not implemented here.
- **gVisor install is best-effort** node userData. If runsc fails to register,
  the worker pod stays Pending (fail-closed).
- **Warm worker, not per-task-ephemeral.** One long-running worker handles tasks
  sequentially (like buildkitd), so there is no per-task isolation reset yet.
  Per-task-ephemeral pods (a dispatcher that creates a Job per request) are Phase 2.
- **Output is trusted as-is.** The propose/dispose validation + approval gate is
  Phase 2.
- **Proxy images are unpinned** (`mitmproxy/mitmproxy:latest`, `aws-sigv4-proxy:latest`) — digest-pin before non-prototype use.
