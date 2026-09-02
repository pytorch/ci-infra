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
- `POST /run` body `{"ref"?,"task"?,"wait"?}` →
  `{"task_id":str,"cloned":bool,"file_count":int,"top_level":[str],"report":str,"errors":{…}}`

  Waits for the task by default, so a caller sees the result on the same connection —
  budget for a cold fleet, where the pod waits on a Karpenter node. `"wait": false`
  returns `202 {"task_id"}` instead. `repo` and `model` are still *parsed* — a non-string
  is a `400` — but neither reaches the Job: the Grant decides both. Supplying either is
  checked rather than ignored, so a `repo` that matches policy is accepted and one that
  does not is a `403`. **`model` is a `403` whatever you send**, because v1's policy model
  is the empty string meaning "the dispatcher's configured default" — including `""`,
  which is compared like any other value rather than skipped. Send neither. See *Who may
  call* below.
  `top_level` is the clone's real top-level listing, which is also fed to the
  model — an empty one means the report was not grounded in the repo.
- `GET /status/<task_id>` → `{"state":"running"}` or `{"state":"done", …result}`.
  Results are kept in memory for an hour after the task finishes. A task belonging to
  another caller answers `404`, not `403` — otherwise the endpoint would confirm that
  other callers are running tasks.

## Who may call, and what a call can do

`/run` authenticates the caller with a **GitHub Actions OIDC token** in an
`Authorization: Bearer` header, and authorizes it against a policy that lives in code —
`dispatcher/authorize.py`, not an env var, because it is the answer to "who may spend our
Bedrock budget" and belongs in git history and review.

v1 admits two callers: a workflow in **`pytorch/ciforge`**, and one in
**`pytorch/ci-infra`** (this module's own `test-agent-sandbox` integration job, the only
thing that calls `/run` today). Either must be on a protected ref, on a **self-hosted**
runner, on an event not in a denied set. The *repository* is matched on
`repository_id`/`repository_owner_id` rather than on its name, because a repository can be
renamed and its old name re-registered by someone else — the two workflow refs are still
matched on a name prefix, so a rename breaks authorization even though the ids resolve.

`self-hosted` is a **shape** check, not a trust boundary: `/run` is a ClusterIP reachable
only from `arc-runners`, so that is simply what a caller who can connect reports. It is not
a defence against untrusted code minting a token — **the client is untrusted by design**,
any job with `id-token: write` can mint one on either kind of runner, and the dispatcher's
job is to validate the token and bound what the validated identity may do. That bound is
the Grant. One residual to keep in view: the event check is a **denylist**, so an event
type GitHub adds later is allowed by default — again bounded by the Grant, not by the set.

**The request decides less than it looks like it does.** Verification produces a frozen
`Grant`, and the Job is built from the Grant alone — never from the request body. The
repository to clone and the model are policy, so a caller cannot name either; passing a
`repo` or a `model` that disagrees with policy is refused outright rather than quietly
substituted. The caller contributes the prompt and the commit to read.

Two residuals worth knowing:

- **The prompt is caller-controlled**, and `workflow_run` is an allowed event, so a
  workflow that reads pull-request content can shape what the agent is asked to do. The
  Grant is what bounds the damage — same repo, same model, same limits.
- **Tokens are replayable until they expire.** `jti` is neither required nor consumed, so
  a stolen token can submit requests until `exp`. Consuming it needs state shared across
  dispatcher replicas, which v1 does not have; the concurrency cap and the namespace quota
  are what bound the damage in the meantime. PyPI's Warehouse solves this with a `jti`
  table, which is the shape to copy if this matters later.

### Enabling enforcement

`REQUIRE_AUTH` still ships **`false`**, but the policy now admits a caller that can
actually reach the endpoint, so flipping it is a real next step rather than an outage. Two
things have to be true first, and neither is code in this repo's dispatcher:

- The **`test-agent-sandbox`** job must send a token. It needs `id-token: write`, a token
  minted for this dispatcher's audience, and an `Authorization: Bearer` header on its
  `curl`. It sends none today, so it would get a `401` the moment the flag flips.
- Any **`pytorch/ciforge`** caller must run on an `arc-runners` runner. Every workflow in
  that repo is `ubuntu-latest`/`ubuntu-24.04` today, and a github-hosted runner cannot
  route to a ClusterIP in this cluster at all.

`test_an_admissible_caller_can_actually_reach_run` asserts the policy and the NetworkPolicy
still describe an overlapping set, so the disjointness that made an earlier revision
unsatisfiable cannot come back unnoticed.

### What the flag does

`REQUIRE_AUTH` in `kubernetes/base/dispatcher.yaml` ships **`false`**. It governs exactly one case: a
request with no `Authorization` header at all. A token that *is* presented is always
verified and always authorized, whatever the flag says, so a forged or denied token is
rejected either way.

Be clear about what that does and does not buy. **While the flag is false, authentication
is optional**, and an unauthenticated caller can therefore do *more* than a caller whose
real token was denied — it simply omits the header. This is a migration window, not a
security posture. It is tolerable only because `/run` is already reachable
unauthenticated by the whole `arc-runners` namespace today, so it is strictly no worse
than the status quo and strictly better once flipped.

Unrecognised values abort at startup rather than defaulting to off: `REQUIRE_AUTH=tru`
under a `== "true"` comparison is a security control disabled by a typo, with no signal
anywhere.

### The signing keys

The dispatcher holds create-Job RBAC, so it is the component that must not be able to
reach the internet — its NetworkPolicy allows DNS and the Kubernetes API and nothing
else. GitHub's signing keys therefore arrive as a mounted ConfigMap, refreshed every six
hours by a CronJob (`kubernetes/base/oidc.yaml`) whose Role can patch that one named
object and nothing more.

The manifest declares that ConfigMap with **no `data`** — the content belongs to the
refresher. That is load-bearing rather than tidy: seeding it in the manifest meant every
`kubectl apply` put the seed back over live keys, so each deploy blanked them. `deploy.sh`
runs a refresh immediately after applying, so the window with no keys is minutes rather
than up to six hours, and the dispatcher fails closed throughout it. Minutes, not seconds,
and not a bound anyone has measured: the Job has to be scheduled and pull an image, the
kubelet then notices the ConfigMap changed on its own sync period, and the dispatcher
re-reads the mount only every `JWKS_RELOAD_INTERVAL_S`.

The refresher writes a `fetched_at` timestamp *into* the document. That is not
decoration: a ConfigMap volume only updates when its content changes and GitHub rotates
rarely, so judging freshness by file mtime would age out a perfectly healthy key set
while a refresher dead for a month looked identical. The dispatcher refuses keys older
than 24h, which is what turns a silently dead refresher into a loud failure.

Task pods declare no volumes at all, and `kubernetes/base/admissionpolicy.yaml` enforces
that in the API server. **Adding one is a policy change as well as a manifest change** —
the `GITHUB_TOKEN` init-container work is the case this is waiting for.

## The two images

`deploy.sh` builds both and pushes them to the in-cluster Harbor `osdc` project
(public, so nodes pull anonymously via the `harbor:30002` mirror) — same pattern as
`modules/zombie-cleanup`. Each tag is a content hash of its own directory, so an
unchanged tree skips the build, a code change deploys a new immutable tag, and editing
one image does not re-roll the other. Requires a local docker daemon.

- `ci-agent-sandbox` (`agent/`) — the untrusted task: `task.py` runs one task and exits,
  `sandbox.py` is the clone + Bedrock library. Holds nothing.
- `ci-agent-sandbox-dispatcher` (`dispatcher/`) — the trusted side: the HTTP surface and
  the Job creation. The task image is stdlib-only; this one is not, and the separate-image
  split is what keeps that from mattering. It installs `python3-jwt` and
  `python3-cryptography` **from apt, not pip**, because GitHub signs its OIDC tokens
  RS256 and the standard library has no public-key crypto at all. None of that reaches
  the sandbox: `agent/` is built from its own Dockerfile and gains nothing from the line.

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
  -d '{"ref":"main","task":"Summarize the build layout"}'

# Or don't hold the connection open:
TASK=$(curl -fsS -X POST http://sandbox-agent.ai-sandbox.svc.cluster.local:8080/run \
  -d '{"wait":false}' | jq -r .task_id)
curl -fsS "http://sandbox-agent.ai-sandbox.svc.cluster.local:8080/status/$TASK"
```
**The caller no longer picks the repository or the model.** Both come from the `Grant`,
and sending either is a `403` rather than a value that is quietly accepted and dropped.
The model is `BEDROCK_DEFAULT_MODEL_ID`, set at deploy time from `clusters.yaml` →
`agent_sandbox.default_model_id`; per-caller models arrive with the capability manifest.

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

`dispatcher/kube.py::job_manifest()` builds every task pod, and
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
- **A task can overwrite the response fields the endpoints own.** `kube.task_result()`
  returns the last `{`-prefixed line the task pod printed that parses as JSON (within
  `MAX_LOG_BYTES`), and both payloads spread it *last* — `{"task_id": task_id,
  **result}` in `http_api.do_POST`, `{"state": "done", "task_id": task_id,
  **task["result"]}` in `tasks.status`. A task printing
  `{"task_id": "…", "state": "running"}` therefore replaces what the dispatcher minted:
  a caller can be told the wrong id, or that a finished task is still running.
  **This needs a schema decision, not a one-line reorder.** Spreading the result first
  protects `task_id`/`state` but then silently drops a task's own fields of those
  names — no merge order preserves both meanings. The two real options are a nested
  envelope (`{"task_id":…, "state":…, "result": result}`, a breaking change for every
  caller) or server-fields-last plus an audit of what callers read today. Deferred for
  that reason; both call sites carry a `KNOWN GAP` comment pointing here.
- **A slot can leak for the life of the pod.** `run_and_record()` releases the slot
  `start_task()` reserved only by reaching `_finish()`, and nothing holds that if the
  runner raises. `_run_to_completion()` catches `(ApiError, OSError)`, but
  `kube._k8s_api()` and `kube._read_token()` raise bare `RuntimeError` from outside
  `api_request()`'s try block, so those escape — including from the `finally:
  kube.delete_job(...)`, which is why a leaked slot does not imply the Job was never
  created. The entry then stays `"running"` forever: `_prune_locked()` only drops
  `"done"` ones and `_running_locked()` keeps counting it against
  `MAX_CONCURRENT_TASKS`. Symptoms are spread out — a waiting `/run` gets no response
  at all and the handler logs a traceback, `/status` answers `"running"` forever,
  `/healthz` shows `in_flight` that never drains, and enough leaks turn every later
  call into a `429`. Reachability is low (an unset `KUBERNETES_SERVICE_HOST`, or the
  projected token file missing at the moment it is read), but the loss is permanent.
  **The fix is not simply a `try/finally` around `_finish()`** — `result` is unbound on
  that path, so it needs a decision about what a crashed task records (a synthetic
  error result, or dropping the reservation) and whether the exception still
  propagates. `run_in_background()` needs its own cleanup rather than the same one: a
  `Thread.start()` that fails leaves the slot reserved with no thread to release it.
- **The proxy image floats** (`aws-sigv4-proxy:latest`) — digest-pin before
  non-prototype use.
- **Callers are unauthenticated in practice, and unbounded either way.** Caller identity
  now exists — `dispatcher/authorize.py`, OIDC-verified — but it is not enforced:
  `REQUIRE_AUTH` ships `false` until the admitted callers actually send tokens (see
  *Enabling enforcement*), so today any pod in `arc-runners` can still call `/run` with no
  token and the NetworkPolicy remains the only real gate.
  Quotas are a separate gap that authentication does not close: a caller looping `/run`
  holds slots for up to the clone plus invoke timeout and keeps every other consumer on
  `429` — a refusal rather than a hang, but still a denial of service. There is no
  per-caller rate or budget limit; the Grant bounds *what* a call may do, never how many.
- **The clone reaches the internet directly.** `sandbox-task-egress` allows TCP 443
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
