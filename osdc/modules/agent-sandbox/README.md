# agent-sandbox (PROTOTYPE)

A sandbox for running an untrusted AI agent in OSDC CI. The agent can *read* from
GitHub and AWS (incl. **Bedrock**) and return results — without ever holding a
credential, and running under gVisor on a dedicated node fleet.

It is **callable over the network like BuildKit**: a runner does
`curl sandbox-agent.ai-sandbox.svc:8080/run …`, gated by a NetworkPolicy that
allows the `arc-runners` namespace (no K8s RBAC on the caller) — exactly how a
runner reaches `buildkitd` on `:1234`.

What works today: gVisor isolation on a dedicated fleet, "use secrets without holding
them" via the signing proxy, BuildKit-style network invocation, and one ephemeral pod
per request so tasks run in parallel with nothing carried over between them. What does
not exist: an output gate on what the agent returns, and a network boundary that
actually holds (see *Limitations*).

## Architecture

```
[arc-runners] runner job ── curl ──► sandbox-agent.ai-sandbox.svc:8080/run
   (no RBAC; NetworkPolicy allow)              │
                                               ▼
   ai-sandbox namespace
   ┌───────────────────────────────────────────────────────────────────────┐
   │ [TRUSTED] sandbox-dispatcher   Deployment on base nodes                 │
   │   • creates ONE Job per /run; RBAC: jobs + pods + pods/log, this ns     │
   │   • no AWS identity, never clones, never prompts a model                │
   │                       │ creates                                         │
   │                       ▼                                                 │
   │ [UNTRUSTED] sandbox-task-<id>   Job, runtimeClassName: gvisor           │
   │   • one task then exits; no credentials, no K8s token                   │
   │   • http ─────────► sigv4-proxy (signs Bedrock with IRSA)               │
   │        pinned to the ai-sandbox gVisor fleet                            │
   └───────────────────────────────────────────────────────────────────────┘
     the credential never shares a node / gVisor sandbox with agent code, and
     the component that can create pods never runs any.
```

Concurrency comes from Karpenter rather than from replicas: N concurrent requests are
N task pods, 3 fit per fleet node, and a pending pod adds one. The ceiling is
`MAX_CONCURRENT_TASKS` per dispatcher replica (2 x 6) and the namespace
`ResourceQuota` (12 slots) behind it — past that a caller gets `429 at capacity`.

- **Isolation:** `nodepools-agent-sandbox` nodes boot from a custom AMI with
  gVisor (runsc) baked in; the `gvisor` RuntimeClass pins task pods there. IMDS
  hop-limit 1 keeps the node role out of reach. Each request gets a fresh pod, so
  nothing carries over between tasks.
- **Credential:** held by the proxy, never by a task. `aws-sigv4-proxy` signs AWS
  requests with a read-only IRSA role (terraform), pinned to Bedrock in this region
  with `--host`/`--name`; tasks send unsigned HTTP. Public repos are cloned directly,
  so there is no GitHub credential at all.
- **Privilege:** the dispatcher can create Jobs in this namespace and nothing else —
  no ClusterRole, no write on pods, no secrets. Task pods run as `sandbox-agent`,
  which has no RBAC and no mounted token.
- **Invocation:** `sandbox-agent` Service (`:8080`), reachable from `arc-runners`
  via `sandbox-agent-ingress` NetworkPolicy — BuildKit parity.

## Endpoints

- `GET /healthz` → `{"status":"ok","in_flight":int,"capacity":int}`
- `POST /run` body `{"repo","ref","task","model"?,"wait"?}` →
  `{"task_id":str,"cloned":bool,"file_count":int,"top_level":[str],"report":str,"errors":{…}}`

  Waits for the task by default, so a caller sees the result on the same connection —
  budget for a cold fleet, where the pod waits on a Karpenter node. `"wait": false`
  returns `202 {"task_id"}` instead.
  `top_level` is the clone's real top-level listing, which is also fed to the
  model — an empty one means the report was not grounded in the repo.
- `GET /status/<task_id>` → `{"state":"running"}` or `{"state":"done", …result}`.
  Results are kept in memory for an hour after the task finishes.

## The two images

`deploy.sh` builds both and pushes them to the in-cluster Harbor `osdc` project
(public, so nodes pull anonymously via the `harbor:30002` mirror) — same pattern as
`modules/zombie-cleanup`. Each tag is a content hash of its own directory, so an
unchanged tree skips the build, a code change deploys a new immutable tag, and editing
one image does not re-roll the other. Requires a local docker daemon.

- `ci-agent-sandbox` (`agent/`) — the untrusted task: `task.py` runs one task and exits,
  `sandbox.py` is the clone + Bedrock library. Holds nothing.
- `ci-agent-sandbox-dispatcher` (`dispatcher/`) — the trusted side: the HTTP surface and
  the Job creation. Separate image so its dependencies never ship inside the sandbox;
  both are stdlib-only today.

## Deploy

```
just deploy-module meta-staging-aws-ue1 nodepools-agent-sandbox   # gVisor fleet
just deploy-module meta-staging-aws-ue1 agent-sandbox             # IRSA + proxy + dispatcher
```

## Use it (from anywhere with cluster network access, e.g. a runner)

```
# -m 900: the call waits for the task, and a cold fleet waits for a Karpenter node.
curl -fsS -m 900 -X POST http://sandbox-agent.ai-sandbox.svc.cluster.local:8080/run \
  -H 'Content-Type: application/json' \
  -d '{"repo":"pytorch/pytorch","ref":"main","task":"Summarize the build layout",
       "model":"us.anthropic.claude-haiku-4-5-20251001-v1:0"}'

# Or don't hold the connection open:
TASK=$(curl -fsS -X POST http://sandbox-agent.ai-sandbox.svc.cluster.local:8080/run \
  -d '{"repo":"pytorch/pytorch","wait":false}' | jq -r .task_id)
curl -fsS "http://sandbox-agent.ai-sandbox.svc.cluster.local:8080/status/$TASK"
```
Any caller can pick the model per request. Omitting `model` falls back to
`BEDROCK_DEFAULT_MODEL_ID`, set at deploy time from `clusters.yaml` →
`agent_sandbox.default_model_id`.

## Capacity

A sandbox slot is **2 vCPU / 4 GiB / 20 GiB disk with requests == limits** (Guaranteed QoS), so
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
cluster-wide daemonset gaining ~200 MiB of requests silently costs a slot.

Concurrency is bounded by the `ResourceQuota` (12 slots = 4 fleet nodes), not by the
node count: Karpenter adds a node when a task pod is pending and takes it back when the
node empties. The trade is latency — a request that has to wait for a new node pays
1–2 minutes before the task starts.

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

Because a `us.` profile routes the request to any US region, the IRSA policy grants
`bedrock:InvokeModel` on `arn:aws:bedrock:*::foundation-model/anthropic.*` in
addition to this region's `us.anthropic.*` inference profiles — with the region
pinned, invokes fail `AccessDenied` whenever routing leaves the cluster's region.

The foundation-model half is conditioned on `bedrock:InferenceProfileArn` matching
one of those profiles, so it authorizes only the routed tail of a profile invoke. A
direct on-demand invoke carries no profile ARN in its request context and is denied,
which makes the profile the only path in — and the model id comes from the caller's
`/run` body, so that boundary is the one bounding what can be invoked.

Changing either half needs **two** checks after `just deploy-module`, because IAM
accepts an unknown condition key silently and a misspelling denies everything:

1. a routed invoke through the profile succeeds (the canary covers this);
2. a direct `anthropic.claude-...` foundation-model invoke returns `AccessDenied`.

One call on its own can't tell a wrong condition key from a wrong request. AWS also
documents an org-level SCP that denies `bedrock:*` when a profile ARN is present but
doesn't match — worth having if this role ever gains other statements that grant
foundation-model access, but it lives outside this repo.
Background: [Securing Amazon Bedrock cross-Region inference](https://aws.amazon.com/blogs/machine-learning/securing-amazon-bedrock-cross-region-inference-geographic-and-global/).

## The task-pod contract, and adding a volume

`dispatcher.py::job_manifest()` builds every task pod, and
`kubernetes/base/admissionpolicy.yaml` restates the same contract in CEL so the API
server rejects a Job that does not match it. The two are deliberately redundant;
`dispatcher/test_admissionpolicy.py` is what keeps them from drifting apart.

**Task pods declare no volumes at all.** That is a rule, not an accident of the current
manifest: a volume is how a Secret, a `hostPath` or a projected service-account token
would get into the untrusted side. An allowlist of safe volume shapes is expressible in
CEL, but it is a rule you can get subtly wrong; "none" is one you cannot. So a change
that needs one — the planned
`GITHUB_TOKEN` init container needs a shared `emptyDir` — is not a one-line edit to
`job_manifest()`. It has to amend the volumes rule and the init-container rule in the
policy, re-pin their expression digests, and say in review which volume types are now
reachable from inside gVisor and why that is acceptable.

## Integration test

Runs as part of the standard canary flow, gated by the `AGENT_SANDBOX` tag
(requires the `agent-sandbox` module). After deploying to staging:
```
just integration-test meta-staging-aws-ue1
```
The `test-agent-sandbox` job runs on a normal runner and `curl`s the sandbox
Service — asserting it is reachable from `arc-runners` (BuildKit parity) and that
it clones a public repo (directly, anonymously) and reaches Bedrock through the
signing proxy, without the runner or worker holding a token.

## Limitations (prototype — read before trusting it)

- **Egress is NOT hard-enforced.** Under IPv6-only AWS VPC-CNI, `NetworkPolicy`
  doesn't cover IPv4 egress (the same gap that made cache-enforcer's node iptables
  unreliable), so the proxies are the *credential* boundary, not a network one: a
  compromised agent still has no token to steal, but can still reach the internet.
  A boundary that would hold — a dedicated sandbox subnet with no NAT/IGW route +
  Security-Groups-for-Pods — does not exist yet.
- **The gVisor AMI must be built per region** before the fleet can launch a node
  (`just build-agent-sandbox-ami <cluster>`), and it pins the AL2023 base — unlike
  the `al2023@latest` fleets it does not pick up CVE fixes on node rotation.
- **A cold fleet is slow to answer.** Task pods are created per request, so when no
  fleet node has a free slot the caller waits on Karpenter (1–2 minutes) before the
  clone even starts. Nothing keeps a node warm.
- **A dispatcher restart loses in-flight waits.** Results live in the dispatcher's
  memory, so a rollout or crash drops the `/status` entry for a task still running; the
  Job finishes regardless and the caller has to retry.
- **Output is trusted as-is.** Nothing validates or gates what a task returns before a
  caller acts on it.
- **The proxy image floats** (`aws-sigv4-proxy:latest`) — digest-pin before
  non-prototype use.
- **Callers are unauthenticated and unbounded.** `/run` has no notion of who is
  asking: any pod in `arc-runners` can call it, and the NetworkPolicy is the only
  gate. With one serial worker, a caller looping `/run` holds the single slot for up
  to the clone plus invoke timeout and keeps every other consumer on `429` — visible
  as a refusal rather than a hang, but still a denial of service. Fine while there
  is one consumer. A caller identity and per-request ephemerality are the same piece
  of work and neither exists — do it before a second consumer does.
- **The clone reaches the internet directly.** `sandbox-agent-egress` allows TCP 443
  to any address because `NetworkPolicy` selects on CIDR and GitHub's ranges move.
  Closing it means git behind a proxy the way Bedrock is, landing together with the
  no-NAT subnet — neither exists, and either alone breaks cloning.
- **Repo context is shallow** — the prompt carries the file count and the
  top-level listing, enough to keep answers grounded, but no file contents. Real
  tasks need reading files (and a tool loop to choose which); today the Bedrock
  call proves the credential path, not agent capability.

## Future direction: vetted MCP services

Secret-backed data sources (ClickHouse, Grafana, CloudWatch) should be exposed as
**vetted MCP servers** that hold the secrets and expose only specific tools — the
same principle as the proxies, at a finer grain than a whole-host allowlist.
