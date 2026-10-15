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
   │   • http ─────────► git-proxy   (adds the GitHub token, private repos)  │
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
  with `--host`/`--name`; tasks send unsigned HTTP. Public repos are cloned directly and
  anonymously; the one GitHub credential lives in `git-proxy`, for private repos only —
  see *Private repositories* below.
- **Privilege:** the dispatcher can create Jobs in this namespace and nothing else —
  no ClusterRole, no write on pods, no secrets. Task pods run as `sandbox-agent`,
  which has no RBAC and no mounted token.
- **Invocation:** `sandbox-agent` Service (`:8080`), reachable from `arc-runners`
  via `sandbox-agent-ingress` NetworkPolicy — BuildKit parity.

## Endpoints

- `GET /healthz` → `{"status":"ok","in_flight":int,"capacity":int}`
- `POST /run` body `{"manifest"?,"repo"?,"ref"?,"pr"?,"base"?,"task"?,"wait"?}` →
  `{"task_id":str,"cloned":bool,"head_sha":str,"file_count":int,"top_level":[str],"report":str,"errors":{…}}`,
  plus `"pr":int` echoed back when the request carried one, and `changed_files`,
  `changed_files_total` and `diff_truncated` when `base` is given. Name lists are cut to
  256 KiB each (`top_level_total` / `changed_files_total` carry the real counts) so the
  result always fits the 1 MiB log it travels in. `head_sha` is the commit actually
  checked out: a branch or a pull request head can move, so pin to it, not to what you
  asked for.

  The model works in a loop (`agent/agent_loop.py`) with three read-only tools over the
  checked-out commit — `list_dir`, `read_file`, `search` — until it answers, or 24 turns
  or 600 s run out (`errors.agent`). The 600 s shrinks to what is left of the task's own
  deadline (`TASK_DEADLINE_S`, passed to the pod as `SANDBOX_DEADLINE`, less 30 s), so
  time lost before the loop no longer lets the loop itself run into the Job deadline. It
  does not protect the stages before the loop, and a node clock more than 30 s off can
  defeat the margin. Tools also stop, and the model is told
  to answer, once the conversation nears a 200K-token window by the model's own count;
  asking for tools again after that ends the loop with `errors.agent`. The first prompt,
  which has no count yet, is sized to fit at one token per byte (the diff is cut first),
  and each model call is bounded by the loop's wall clock as a whole. Tools read through git (`HEAD:<path>`), never the
  filesystem. The result also carries `turns` and `tool_calls`.

  When a budget refuses a tool call — 120 calls, 768 KiB of tool output, the context
  window, time (refused while 60 s are still left, which no read may spend) or turns (the
  last is kept for the answer) — and
  the model then answers from what it has read, the answer is returned with
  `tools_refused` naming that budget, so it can be told from one written after complete
  reading. (`tools_refused` is set whenever the model was told its budget is spent, which
  with effects allowed can happen without a refused call.) A turn may use up to 16000 tokens, thinking included, and
  300 s. A throttled (429), failed (5xx) or dropped model call is retried up to three
  times with backoff while at least 30 s of the loop's time is left; a call that still
  fails ends the run with `errors.bedrock`, keeping `turns` and `tool_calls`.

  `ref` is a branch, a tag, `refs/pull/<n>/head` or a commit sha, fetched at depth 1. A
  sha must be the full 40 hex characters — an abbreviated one is fetched as a ref name
  and fails. Anything that is not a plain ref name (a refspec, a range, an option) is a
  `400`. **`"pr": <number>` checks out that pull request's head** (`refs/pull/<n>/head`)
  instead of `ref`, which it overrides. Like `ref`, `pr` is a *selector*: it names a pull
  request **of the repository the Grant clones**, so it cannot reach another repo. PR head
  refs live in the base repository, so this reaches a fork's pull request without naming
  the fork.

  `base` is an optional full, lowercase commit sha: the task fetches it too and puts
  `git diff base <checked-out commit>` (bounded) in front of the model, so for a pull
  request pass the merge base. Without it the model gets only the top-level listing. A
  diff that cannot be computed stops the task with `errors.diff` rather than producing a
  review of nothing.

  `manifest` names the capability manifest the call is made under; it is required with a
  token. `repo` chooses among the repositories that manifest allows (the first is the
  default); the model is the manifest's, and a `model` that disagrees is a `403`.
  Waits for the task by default — budget for a cold fleet, where the pod waits on a
  Karpenter node. `"wait": false` returns `202 {"task_id"}` instead.
  `top_level` is the clone's real top-level listing, which is also fed to the
  model — an empty one means the report was not grounded in the repo.
- `GET /status/<task_id>?manifest=<name>` → `{"state":"running"}` or
  `{"state":"done", …result}`. Results are kept in memory for an hour after the task
  finishes. A task is owned by its caller *and* manifest; one belonging to anyone else
  answers `404`, not `403`, so the endpoint does not confirm that other tasks exist.

## Who may call, and what a call can do

`/run` authenticates the caller with a **GitHub Actions OIDC token** in an
`Authorization: Bearer` header and authorizes it against a **capability manifest**: one
YAML file per use case in `kubernetes/base/capabilities/`, deployed as a ConfigMap and
parsed by `dispatcher/manifest.py`. The request names the manifest; `authorize.py` checks
the token against that manifest's `clients` region and builds the Grant from the rest.

```yaml
name: ciforge-pr-review
owner: pytorch-dev-infra
clients:
  repos:                     # matched on the ids; the name must still match the workflow refs
    - repository: pytorch/ciforge
      repository_id: "1133856973"
      repository_owner_id: "21003710"
  triggers: [pull_request, workflow_run, workflow_dispatch]   # the token's event_name
  workflows: []              # optional: file names allowed to call, at any ref; empty = any
model:
  id: us.anthropic.claude-opus-5-5   # optional; empty = the dispatcher's default
sandbox:
  repos: [pytorch/ciforge, pytorch/pytorch]   # repos the task may clone; first = default
```

**The manifest is the trust anchor, not the client.** A manifest changes only through a
reviewed commit on this repository's default branch plus a deploy, so a pull request
cannot edit the manifest it is judged against. That is why a PR-triggered client is
allowed where its manifest lists `pull_request`, and why the caller's own branch need not
be protected: the Grant bounds what any admitted caller can do — same repositories, same
model, same limits — whoever wrote the workflow. The loader is strict (unknown keys, empty
required lists and integer ids are errors) and the dispatcher refuses to start without
manifests.

Both workflow refs in the token must be inside the client repository, and `job_workflow_ref`
is required, so an allowed repository cannot delegate its identity to a reusable workflow
living elsewhere. The token's `runner_environment` must be one the manifest lists in
`clients.runner_environments` (default `[self-hosted]`). That is a **shape** check
(`/run` is a ClusterIP reachable only from `arc-runners`), not a trust boundary; a
manifest opts in to `github-hosted` once there is a public endpoint
([docs/public-endpoint.md](docs/public-endpoint.md)).

Two residuals worth knowing:

- **The prompt is caller-controlled**, so a workflow that reads pull-request content can
  shape what the agent is asked to do. The Grant is what bounds the damage.
- **Tokens are replayable until they expire.** `jti` is neither required nor consumed;
  the concurrency cap and the namespace quota bound the damage meanwhile.

### Enforcement

`REQUIRE_AUTH` in `kubernetes/base/dispatcher.yaml` ships **`true`**: a request with no
`Authorization` header gets `401`. The **`test-agent-sandbox`** integration job sends a
fresh token per request under the `osdc-integration-test` manifest, and asserts that a
request without one is refused.

The flag governs exactly one case, a request with no `Authorization` header at all. A
token that *is* presented is always verified and always authorized, whatever the flag
says. Setting it to `"false"` is a rollback switch, not a posture: an unauthenticated
caller then gets the v1 Grant (`authorize.V1_CLONE_REPO`, the default model), which is
*more* than a caller whose real token was denied, and `/run` reopens to every pod in
`arc-runners`.

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
re-reads the mount only every `JWKS_RELOAD_INTERVAL_S`. If that refresh fails on a
cluster with no keys yet, the deploy fails (after its other steps) rather than report
success while every call is refused — unless the Deployment was applied with
`REQUIRE_AUTH: "false"` (the rollback), which still serves callers without a token, so it
only warns; with a key set the dispatcher will still accept for at least another hour in
place, it warns and keeps that set.

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

## Use it (from a workflow job on an OSDC runner)

Prefer the action below. By hand, from a job with `permissions: id-token: write` whose
repository, trigger and workflow a manifest admits:

```
SANDBOX=http://sandbox-agent.ai-sandbox.svc.cluster.local:8080
token() {   # a fresh one per call: they expire within minutes
  curl -fsS -H "Authorization: bearer $ACTIONS_ID_TOKEN_REQUEST_TOKEN" \
    "$ACTIONS_ID_TOKEN_REQUEST_URL&audience=agent-service" | jq -r .value
}
# -m 900: the call waits for the task, and a cold fleet waits for a Karpenter node.
curl -fsS -m 900 -X POST "$SANDBOX/run" -H "Authorization: Bearer $(token)" \
  -H 'Content-Type: application/json' \
  -d '{"manifest":"ciforge-experiments","ref":"main","task":"Summarize the build layout"}'

# Review a pull request of the manifest's repository: `pr` checks out its head, and
# `base` (the merge base) hands the agent the diff; without `base` it sees the tree only.
curl -fsS -m 900 -X POST "$SANDBOX/run" -H "Authorization: Bearer $(token)" \
  -H 'Content-Type: application/json' \
  -d '{"manifest":"ciforge-experiments","pr":1234,"base":"<merge-base sha>","task":"What does this change touch?"}'

# Or don't hold the connection open; /status needs the same identity and manifest:
TASK=$(curl -fsS -X POST "$SANDBOX/run" -H "Authorization: Bearer $(token)" \
  -d '{"manifest":"ciforge-experiments","wait":false}' | jq -r .task_id)
curl -fsS -H "Authorization: Bearer $(token)" "$SANDBOX/status/$TASK?manifest=ciforge-experiments"
```
**The caller does not pick the model**, and picks the repository only among those its
manifest allows. The model is the manifest's `model.id`, or `BEDROCK_DEFAULT_MODEL_ID`
(set at deploy time from `clusters.yaml` → `agent_sandbox.default_model_id`) when the
manifest leaves it empty or the call is unauthenticated.

## Private repositories: the git credential proxy

A task pod holds no GitHub credential, so an anonymous fetch reaches **public
repositories only** — a private one fails with `could not read Username`. `git-proxy`
is the answer, and it is the same shape as `sigv4-proxy`: the credential lives in the
proxy, the agent sends an unauthenticated request, and the proxy authenticates it on
the way out. The agent never learns the token.

Two properties do the security work, and neither is the token's own scope:

- **An allowlist**, a literal in `kubernetes/base/git-proxy.yaml`. A proxy that
  forwarded any path with a token attached would let anything that can reach it read
  every repo that token can. Add a repo there, in review, the way a manifest grants
  one.
- **Read only.** Only the two endpoints `git fetch` uses are routed; `git-receive-pack`
  is not a location and `service=git-receive-pack` is refused, so a token that happens
  to carry write access cannot push through it.

The credential is **not** created by `deploy.sh` — it is a GitHub token, and the deploy
has no business minting one. Create it out of band:

```
TOKEN=<a token with contents:read on the allowlisted repos>
kubectl create secret generic git-proxy-credentials -n ai-sandbox \
  --from-literal=basic-auth="$(printf 'x-access-token:%s' "$TOKEN" | base64 | tr -d '\n')"
```

Pre-encoded because git over HTTPS authenticates with Basic and nginx cannot base64 at
render time. A GitHub App installation token is the better source than a PAT — an hour
long and scoped per repo — but it needs a refresher, which this does not yet have.
`pytorch/ciforge` is granted by both ciforge manifests and listed in
`kube.PRIVATE_REPOS`, so an authenticated ciforge caller clones it through the proxy.
Only repositories in `PRIVATE_REPOS` are routed that way — a public clone goes straight
to github.com, so the proxy being down or unconfigured cannot break one. Every private
repository a manifest grants must be in `PRIVATE_REPOS` and in the nginx allowlist:
granted but not routed fetches anonymously and 404s, routed but not allowlisted gets a
403 (`test_authorize` and `test_dispatcher` check both pairings).

## Calling it from a workflow

`action/` is a composite action that mints the job's OIDC token, calls `/run` under a
manifest, and exposes `task-id`, `report` and `result-file` as outputs. It retries only a
`429` (jittered backoff under `max-wait` seconds), never a failure after the task was
admitted. Pin it by commit sha; it runs with the calling job's identity.

```yaml
permissions:
  id-token: write
steps:
  - uses: pytorch/ci-infra/osdc/modules/agent-sandbox/action@<sha>
    id: sandbox
    with:
      manifest: ciforge-pr-review
      repo: pytorch/pytorch
      ref: main
      task: "Summarize the build system."
  # The report is model output: write it to the step summary, never echo it into the
  # log, where a line starting with `::` would be read as a workflow command. Read it
  # from the result file: Linux caps one environment variable at 128 KiB, and a long
  # report passed through `env:` would stop the step from starting.
  - run: jq -r .report "$RESULT_FILE" >> "$GITHUB_STEP_SUMMARY"
    env:
      RESULT_FILE: ${{ steps.sandbox.outputs.result-file }}
```

To review a pull request, pass its head sha as `ref` (or `refs/pull/<n>/head`, the ref
the dispatcher checks out when given a `pr` number) and the merge base as `base`, so the
agent is given the diff.

## Writes: proposed in the sandbox, applied outside it

The agent never writes. When its manifest lists `capabilities.effects`, the agent gets a
`propose_effect` tool and can propose a PR comment or a check run; the task returns the
proposals in `effects`. The dispatcher screens them against the Grant
(`dispatcher/effects.py`): the kind must be listed, bodies fit `max_bytes`, a check run's
conclusion comes from the manifest's set and its name from the manifest. Effects need the
request's `ref` to be a commit sha, and the task must report having checked out exactly
that commit — so a run selected only by `pr` gets no effects; pass the head sha as `ref`.
Every accepted effect is pinned to that commit and to the cloned repository, and opens
with a provenance line. Every `@pytorchbot` or `@pytorchmergebot` mention in a body is
put in a code span, so pytorch-bot cannot read it as a command. Rejections are reported
in `errors.effects` (a warning, not a failed step); a run that did not finish proposes
nothing.

The tool is offered each effect's `max_bytes` less the provenance line, and checks a
check run's title as screening does, so a proposal it accepts is not dropped for size
afterwards. Proposing is how the agent delivers its answer, so it stays open after the
read budget is spent: once the agent has seen that — a refused read, or a note on the
result of the call that spent the budget — it may propose in one more turn, and any tool
call after that ends the run. Two turns, and twice the time reserve (120 s), are kept back
for this (the proposal and the answer), so neither limit cuts it short.

The action applies what survives when called with `apply-effects: "true"`, `wait: "true"`
and a `pr-number` (`action/apply_effects.py`), reading the result its own `/run` step just wrote
in the same job, using the `github-token` input. That defaults to the job's token, which
can write only to the calling repository; a caller that reviews another repository must
pass a token for it (a GitHub App installation token), and the step refuses to start with
the job's token otherwise. A comment is posted as a pull-request review with
`commit_id` set to the reviewed commit, a check run is created on that commit, effects for
another repository are refused, and nothing is written if the pull request has moved on.
For PR-triggered callers, run the whole call — `/run` and the apply step — in a
`workflow_run` job on the default branch, fed only the PR number by the untrusted stage;
never apply an artifact produced by another job.

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
Service — asserting it is reachable from `arc-runners` (BuildKit parity), that a call
without an OIDC token is refused and one under `osdc-integration-test` is admitted, and
that the task clones a public repo (directly, anonymously — public clones do not use
git-proxy) and reaches Bedrock through the signing proxy. The runner holds only its OIDC
token for the dispatcher; the task pod holds no credential at all.

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
- **Callers are authenticated, but unbounded.** Every call carries a verified OIDC token
  (see *Enforcement*).
  Quotas are a separate gap that authentication does not close: a caller looping `/run`
  holds slots for up to the task deadline (`TASK_DEADLINE_S`, 900 s) and keeps every other consumer on
  `429` — a refusal rather than a hang, but still a denial of service. There is no
  per-caller rate or budget limit; the Grant bounds *what* a call may do, never how many.
- **A pull request head is untrusted content, and it reaches the prompt.** The
  top-level listing, the diff and every file the agent's tools read come out of the
  checked-out tree, so with `pr` set, content authored by whoever opened the pull
  request — a fork contributor, not a caller — is in front of the model verbatim.
  Nothing filters it; fencing it would not help, because the model reads the whole
  prompt either way. What bounds it is that the tools only read the checked-out commit
  and the agent holds no credentials, so the worst outcome is a misleading report
  returned to the caller — or, where the manifest lists effects, a misleading comment or
  neutral check on the pull request under review, which is all screening lets through.
- **git-proxy authorizes on repository, not on caller.** `repo_allowed` matches the URL
  path, and `git-proxy-ingress` admits every pod labelled `app: sandbox-task` — which is
  every task pod, whatever `Grant.clone_repo` its caller was issued. So a task dispatched
  by one allowed caller can reach any repo on the proxy's allowlist, not just its own.
  Not reachable today: the task image clones `SANDBOX_REPO` and nothing else, and the
  model has no tool that runs commands. It becomes reachable the moment a task can
  execute arbitrary code, which is what the agentic option would add. Closing it means a
  per-Job marker the dispatcher sets and the task cannot forge, or having the dispatcher
  fetch the pack and hand it over — the same shape as the `GITHUB_TOKEN` init-container
  note above.
- **The clone reaches the internet directly.** `sandbox-task-egress` allows TCP 443
  to any address because `NetworkPolicy` selects on CIDR and GitHub's ranges move.
  Closing it means git behind a proxy the way Bedrock is, landing together with the
  no-NAT subnet — the proxy now exists, the subnet does not, and the subnet alone
  still breaks cloning.
- **Repo context is shallow** — the prompt carries the file count and the
  top-level listing, enough to keep answers grounded, but no file contents. Real
  tasks need reading files (and a tool loop to choose which); today the Bedrock
  call proves the credential path, not agent capability.

## Future direction: vetted MCP services

Secret-backed data sources (ClickHouse, Grafana, CloudWatch) should be exposed as
**vetted MCP servers** that hold the secrets and expose only specific tools — the
same principle as the proxies, at a finer grain than a whole-host allowlist.
