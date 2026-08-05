# agent-sandbox (PROTOTYPE)

A sandbox for running an untrusted AI agent in OSDC CI. The agent can *read* from
GitHub, AWS (incl. **Bedrock**), CloudWatch/Logs, and *propose* results — without
ever holding a credential, and running under gVisor on a dedicated node fleet.

This is a **Phase-1 PoC**: it proves the security spine end to end (gVisor
isolation + "use secrets without holding them" + git-clone/Bedrock through
proxies). The warm-pool dispatcher, VPC-layer egress lockdown, and the
structured propose/dispose actuator are **deferred** (see *Limitations*).

## Architecture

```
[trusted] operator / GitHub Action ── run-agent.sh ──► kubectl create Job
                                                              │
   ai-sandbox namespace                                       ▼
   ┌───────────────────────────────────────────────────────────────────────┐
   │ [UNTRUSTED] agent Job pod   runtimeClassName: gvisor, single-use        │
   │   • no credentials, no K8s token                                        │
   │   • HTTPS_PROXY ─► agent-vault  (injects read-only GitHub token on wire)│
   │   • http ─────────► sigv4-proxy (signs Bedrock/AWS with IRSA)           │
   │   • writes /output (plain text)                                         │
   │        pinned to the ai-sandbox Karpenter fleet (gVisor nodes)          │
   └───────────────────────────────────────────────────────────────────────┘
        agent-vault + sigv4-proxy run on base nodes (OFF the sandbox fleet):
        the credential never shares a node / gVisor sandbox with agent code.
```

- **Isolation:** `nodepools-agent-sandbox` fleet installs gVisor (runsc) via node
  userData; the `gvisor` RuntimeClass pins agent pods there.
- **Credentials:** `agent-vault` (Infisical Agent Vault) header-injection proxy
  for GitHub/Grafana/ClickHouse; `aws-sigv4-proxy` for AWS/Bedrock via a
  read-only IRSA role (terraform). Agent holds neither.
- **Network:** `NetworkPolicy` restricts agent egress to the two proxies + DNS
  (best-effort only under IPv6 VPC-CNI — see *Limitations*).

## One-time prerequisites (operator, out-of-band)

1. **Build + push the agent image**, then set `agent_sandbox.agent_image` in
   `clusters.yaml`:
   ```
   docker build -t <registry>/ci-agent-sandbox:prototype modules/agent-sandbox/agent
   docker push  <registry>/ci-agent-sandbox:prototype
   ```
2. **Create the two Secrets** in `ai-sandbox` (NOT committed):
   - `agent-sandbox-creds` — the injected credentials (read-only!):
     ```
     kubectl create secret generic agent-sandbox-creds -n ai-sandbox \
       --from-literal=github-token=<READ-ONLY, single-repo GitHub App token>
     ```
   - `agent-vault-ca` — the MITM CA the agent trusts (generate once):
     ```
     openssl req -x509 -newkey rsa:2048 -nodes -days 365 \
       -keyout ca.key -out ca.crt -subj "/CN=agent-vault-ca"
     kubectl create secret generic agent-vault-ca -n ai-sandbox \
       --from-file=ca.crt=ca.crt --from-file=ca.key=ca.key
     ```
3. **Verify the upstream proxy specifics** (this is preview-stage software):
   pin `infisical/agent-vault` + `aws-sigv4-proxy` by digest and confirm Agent
   Vault's `config.json` schema / CLI flags / listen port against the pinned
   version (see `kubernetes/base/agent-vault.yaml`).

## Deploy

```
just deploy-module meta-staging-aws-ue1 nodepools-agent-sandbox   # gVisor fleet
just deploy-module meta-staging-aws-ue1 agent-sandbox             # IRSA + proxies
```
`agent-sandbox` runs terraform (sigv4 IRSA role) → applies the namespace,
RuntimeClass, SAs, proxies, and NetworkPolicies → annotates the sigv4-proxy SA.

## Run a task

```
modules/agent-sandbox/run-agent.sh meta-staging-aws-ue1 \
  pytorch/pytorch main \
  "Summarize the top-level build layout" \
  anthropic.claude-3-5-sonnet-20240620-v1:0
kubectl logs -n ai-sandbox -l app=sandbox-agent -f
```

## Limitations (prototype — read before trusting it)

- **Egress is NOT hard-enforced.** Under IPv6-only AWS VPC-CNI, `NetworkPolicy`
  doesn't cover IPv4 egress (the same gap that made cache-enforcer's node
  iptables unreliable). The real boundary — a dedicated sandbox subnet with no
  NAT/IGW route + Security-Groups-for-Pods — is **Phase 2**, not implemented here.
- **gVisor install is best-effort** node userData. If runsc fails to register,
  agent pods stay Pending (fail-closed). To iterate without it, drop
  `runtimeClassName` from the Job and pin to the fleet manually.
- **Output is trusted as-is.** The propose/dispose validation + approval gate is
  Phase 2; today the agent just dumps text to `/output`.
- **No warm pool / dispatcher.** Each run is a fresh Job (cold start). The
  BuildKit-style warm pool is Phase 2.
- **Upstream proxies are preview-stage.** Vendor + digest-pin before any
  non-prototype use.
