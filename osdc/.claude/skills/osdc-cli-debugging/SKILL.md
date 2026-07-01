---
name: osdc-cli-debugging
description: >
  Read-only CLI commands for debugging OSDC clusters: kubectl, aws, helm, tofu.
  Includes command references, safety boundaries, and the deploy-audit ConfigMaps
  in osdc-system (what version is running where, who deployed it, when, did it succeed,
  is a deploy stuck).
  Applies to ~/meta/ci-infra/osdc.
  Load when investigating cluster state, debugging pods, inspecting infrastructure,
  comparing deployed versions across clusters, or diagnosing a failed or stuck deploy.
---

# OSDC CLI Debugging (Read-Only)

**You are encouraged to run read-only CLI commands to debug, investigate, and understand cluster and infrastructure state.** This is the fastest way to diagnose issues — use it proactively. All tools below are managed by mise via `osdc/mise.toml`.

Run commands from the `osdc/` working directory so mise activates the correct tool versions. If running from elsewhere, prefix with `mise exec --`:

```bash
mise exec -- kubectl get pods -n arc-runners
```

## clusters.yaml Location

All clusters are defined in `clusters.yaml` in the project root. Run `just list` for full details. Read the file directly for config values.

The `<cluster>` argument to every `just` recipe is the **YAML key** (e.g. `meta-prod-aws-ue1`, `meta-staging-aws-uw1`, `arc-cbr-production`, `arc-cbr-production-uw1`), not the `cluster_name` field — they sometimes differ (e.g. `arc-cbr-production` resolves to `cluster_name: pytorch-arc-cbr-production`).

## lf-* Clusters Are NOT Accessible From This Laptop

The `lf-prod-aws-ue1` / `lf-prod-aws-ue2` clusters (state buckets `lf-osdc-tfstate-prod-*`, admin role `lf_osdc_admin`) are operated by another org. **From this laptop the operator has no AWS credentials, no kubeconfig access, and no tofu state read access for them.** Any `just kubeconfig lf-prod-*`, `kubectl` against an `lf-*` context, `aws eks describe-cluster --name lf-prod-*`, or `tofu init` against an `lf-osdc-*` state bucket will fail on authentication.

Accessible from this laptop (admin role `osdc_gha_prod` / `osdc_gha_staging`):
- `meta-staging-aws-uw1` (us-west-1)
- `meta-staging-aws-ue1` (us-east-1)
- `meta-prod-aws-ue1` (us-east-1)
- `arc-cbr-production` → `pytorch-arc-cbr-production` (us-east-2)
- `arc-cbr-production-uw1` → `pytorch-arc-cbr-production-uw1` (us-west-1)

For lf-* investigations: read `clusters.yaml`, read the module code, read the manifests in `base/` and `modules/` — but do not attempt live cluster commands. If the user explicitly asks for lf-* live state, tell them you cannot reach the cluster and ask them to run the command and paste output.

## Kubeconfig: Use `just kubeconfig <cluster>` (MANDATORY)

Before running any kubectl or helm commands, configure kubeconfig using the just recipe — **never run `aws eks update-kubeconfig` directly.**

```bash
just kubeconfig <cluster>
```

The just recipe reads `clusters.yaml` to resolve the cluster name and region automatically and delegates to `scripts/kubeconfig-lock.sh` (which serializes concurrent kubeconfig writes via a file lock). It aliases the kubectl context to the **cluster_name** (e.g. `pytorch-arc-cbr-production`, `meta-prod-aws-ue1`, `meta-staging-aws-uw1`) — there is no `eks-...` prefix. Switch contexts with `kubectl config use-context <cluster_name>` or scope a single call with `kubectl --context <cluster_name> ...`. Running `aws eks update-kubeconfig` by hand risks wrong cluster name, wrong region, or wrong kubeconfig context.

## Corporate Proxy Bypass for kubectl / aws / EKS (MANDATORY)

This sandbox inherits Meta's x2p proxy (`HTTPS_PROXY=http://localhost:10054`), which blocks the EKS API endpoints. **Every ad-hoc `kubectl`, `aws eks`, or `helm` call against the cluster MUST inline the bypass** — `export` does not persist across separate Bash tool invocations, so prefix each command:

```bash
NO_PROXY="${NO_PROXY:-},.eks.amazonaws.com" no_proxy="${no_proxy:-},.eks.amazonaws.com" kubectl get pods -A
NO_PROXY="${NO_PROXY:-},.eks.amazonaws.com" no_proxy="${no_proxy:-},.eks.amazonaws.com" aws eks describe-cluster --name <cluster-name> --region <region>
```

The shipped recipes (`just kubeconfig`, `just deploy-*`, `scripts/destroy-cluster.sh`, etc.) already inject the bypass. The raw kubectl/aws/helm examples in the sections below omit the prefix only for readability — you still must add it when you actually run them.

`aws` calls that do NOT touch the EKS API endpoint (`aws s3 ...`, `aws ec2 describe-*`, `aws autoscaling ...`, `aws ecr ...`, `aws iam ...`, etc.) generally work without the bypass, because Meta's proxy already whitelists the regular AWS service endpoints. Add the bypass when in doubt — it is harmless on non-EKS calls.

## Where Modules Deploy (Namespace Map)

| Namespace | What lives there |
|-----------|------------------|
| `arc-systems` | ARC controller + per-runner-set listener pods |
| `arc-runners` | Runner pods, AutoscalingRunnerSets, zombie-cleanup CronJob |
| `karpenter` | Karpenter controller |
| `harbor-system` | Harbor + harbor-cache-recovery CronJob |
| `buildkit` | BuildKit builder pods |
| `monitoring` | Alloy metrics + kube-prometheus-stack (default; overridable via `monitoring.namespace` in `clusters.yaml`) |
| `logging` | Alloy logging + Alloy events (default; overridable via `logging.namespace` in `clusters.yaml`) |
| `pypi-cache` | PyPI wheel cache pods |
| `kube-system` | node-compactor, cache-enforcer, NVIDIA device plugin, registry mirror config, algif mitigation, dirtyfrag mitigation, node-performance-tuning, image-cache-janitor, node-local-dns DaemonSet (see `base/kubernetes/` for the full list) |
| `osdc-system` | Deploy-audit ConfigMaps (labeled `app.kubernetes.io/managed-by=osdc-deploy-log`) read by `just deploy-status` and `just deploy-history` |

## kubectl (Kubernetes)

```bash
kubectl get nodes                                    # List nodes and status
kubectl get pods -A                                  # All pods across namespaces
kubectl get pods -n arc-runners                      # Runner pods
kubectl get pods -n arc-systems                      # ARC controller pods
kubectl get pods -n karpenter                        # Karpenter pods
kubectl get pods -n harbor-system                    # Harbor pods
kubectl get pdb -n harbor-system                     # list Harbor PDBs and disruption budgets
kubectl describe pdb harbor-core -n harbor-system    # see selector matches and current/desired healthy
kubectl get pods -n buildkit                         # BuildKit builder pods
kubectl get pods -n monitoring                       # Alloy metrics + kube-prometheus-stack pods
kubectl get pods -n logging                          # Alloy log collector pods
kubectl get pods -n pypi-cache                       # PyPI wheel cache pods
kubectl get ds -n logging                            # Logging DaemonSet (should match node count)
kubectl get nodepools                                # Karpenter NodePools
kubectl get nodeclaims                               # Karpenter NodeClaims (what Karpenter has provisioned)
kubectl get ec2nodeclasses                           # Karpenter EC2NodeClasses (AMI / IAM / tag config)
kubectl get autoscalingrunnersets -n arc-runners     # ARC runner scale sets (one per runner type, named arc-<runner_name>)
kubectl get pods -n arc-systems -l app.kubernetes.io/component=runner-scale-set-listener  # ARC listener pods
kubectl describe pod <pod> -n <ns>                   # Pod details and events
kubectl logs <pod> -n <ns>                           # Pod logs
kubectl get events -n <ns> --sort-by=.lastTimestamp  # Recent events
kubectl top nodes                                    # Node resource usage
kubectl top pods -n <ns>                             # Pod resource usage
kubectl get pods -A --field-selector=status.phase=Pending  # All pending pods (often Karpenter is missing capacity)
kubectl get pods -A --sort-by=.status.containerStatuses[0].restartCount  | tail -20  # Top restart counts (crashloop suspects)
kubectl get pods -A -o wide | awk '$4 != "Running" && $4 != "Completed" && NR>1'   # Anything not Running/Completed
```

## aws (AWS CLI)

```bash
aws eks describe-cluster --name <cluster-name> --region <region>
aws eks list-access-entries --cluster-name <cluster-name> --region <region>             # Cluster auth entries (who can get in)
aws eks describe-access-entry --cluster-name <cluster-name> --principal-arn <arn> --region <region>
aws ec2 describe-instances --filters "Name=tag:eks:cluster-name,Values=<cluster-name>" --query 'Reservations[].Instances[].{ID:InstanceId,Type:InstanceType,State:State.Name,AZ:Placement.AvailabilityZone,Launch:LaunchTime}' --output table --region <region>
aws ec2 describe-nat-gateways --filter "Name=tag:Cluster,Values=<cluster-name>" --query 'NatGateways[].{ID:NatGatewayId,State:State,Subnet:SubnetId}' --output table --region <region>
aws ec2 describe-vpc-endpoints --filters "Name=tag:Cluster,Values=<cluster-name>" --query 'VpcEndpoints[].{ID:VpcEndpointId,Service:ServiceName,State:State}' --output table --region <region>
aws ecr describe-repositories --region <region>
aws autoscaling describe-auto-scaling-groups --query 'AutoScalingGroups[].{Name:AutoScalingGroupName,Desired:DesiredCapacity,Min:MinSize,Max:MaxSize}' --output table --region <region>
aws s3 ls s3://<state_bucket>/ --recursive | head                                       # Peek at tofu state object layout for a cluster
```

Use the access-entries commands when troubleshooting "why can't I get into this cluster" — `clusters.yaml` has an `access_config` block that controls EKS access entries.

For Karpenter-provisioned nodes the source of truth is the `NodeClaim` CR (`kubectl get nodeclaims`), not `aws autoscaling` — Karpenter nodes are NOT in an ASG. ASGs only back the **base** node group (provisioned by EKS managed node groups via tofu). `aws ec2 describe-instances` will show both kinds.

## helm

```bash
helm list -A                                         # All installed releases
helm list -n arc-runners                             # Per-namespace (arc-runners has many: arc-<runner_name> per runner type)
helm status <release> -n <ns>                        # Release status
helm get values <release> -n <ns>                    # Current values
helm history <release> -n <ns>                       # Release history
```

## just (read-only recipes)

```bash
just list                                            # All clusters with details
just show <cluster>                                  # Dry-run: cluster name, region, bucket, modules, tofu vars
just plan <cluster>                                  # Tofu plan (no apply, no k8s/helm side effects — safe from CI on PRs)
just deploy-status <cluster> [module]                # Deployed versions for cluster (reads ConfigMaps in osdc-system, labeled app.kubernetes.io/managed-by=osdc-deploy-log). Optional second arg narrows to one module.
just deploy-history <cluster>                        # Recent deploy history (same ConfigMap source)
```

`just deploy-status` / `just deploy-history` require kubeconfig to be set first (`just kubeconfig <cluster>`) — they read live ConfigMaps from the cluster.

## Deploy-Audit ConfigMaps (osdc-system)

Every `just deploy`, `just deploy-base`, and `just deploy-module` records itself as ConfigMaps in the `osdc-system` namespace via `scripts/deploy-log.sh`. These are the source of truth for **what version is running where, who deployed it, when, did it succeed, and is something stuck**. The `osdc-system` namespace exists for nothing else — it is purely the audit log.

**Always start here**: `just deploy-status <cluster>` — it parses the ConfigMaps and prints color-coded Current Versions + recent history. Drop to raw kubectl only when you need a field the renderer hides (`branch`, `duration`) or want to diff across clusters.

### What gets written

Every recipe writes three ConfigMaps per recorded event, all labelled `app.kubernetes.io/managed-by=osdc-deploy-log`:

| Name pattern | When written | Status field |
|---|---|---|
| `osdc-deploy-<scope>-start-<name>` | Immediately when the recipe begins | `started` |
| `osdc-deploy-<scope>-finish-<name>` | When the recipe exits (success or via ERR trap on failure) | `completed` or `failed` |
| `osdc-deploy-<scope>-history-<name>` | Both events appended to `.data.entries` (JSONL, trimmed to last 50 lines) | n/a |

`<scope>` is `cmd` for the top-level `just` recipe and `module` for the per-module record. `<name>` is the recipe or module name. Examples:
- `just deploy meta-prod-aws-ue1` → `osdc-deploy-cmd-{start,finish,history}-deploy`
- `just deploy-base meta-prod-aws-ue1` → `osdc-deploy-cmd-{start,finish,history}-deploy-base`
- `just deploy-module meta-prod-aws-ue1 karpenter` → writes **two** records: `osdc-deploy-cmd-{...}-deploy-module-karpenter` AND `osdc-deploy-module-{...}-karpenter`

A full `just deploy` of an N-module cluster emits one outer `deploy` record, one `deploy-base` record, and 2N module records (cmd-scope + module-scope per module) — roughly 100 ConfigMaps for a 16-module cluster.

### Data schema

Common keys on both `start` and `finish`:

| Key | Source | Example |
|---|---|---|
| `commit` | `git rev-parse --short HEAD` on the deploy machine (or `unknown`) | `4fe8f56` |
| `branch` | `git branch --show-current` (or `detached` for SHA checkouts; CI is almost always `detached`) | `main` |
| `user` | `$USER` on the deploy machine (NOT the GH actor) | `jschmidt` |
| `cluster` | The positional arg passed to the `just` recipe | `meta-prod-aws-ue1` |
| `timestamp` | `date -u +%Y-%m-%dT%H:%M:%SZ` — ISO-8601 UTC, second precision, lexicographically sortable | `2026-06-30T14:23:45Z` |
| `module` or `command` | The recipe/module name | `karpenter` / `deploy-base` |
| `status` | `started` (on start CM); `completed` or `failed` (on finish CM) | `completed` |

Only on `finish`: `duration` — wall-clock seconds as a string (e.g. `"197"`).

History entries are `\n`-separated JSON lines with the same fields plus `event: start|finish`.

### Debugging recipes

All commands assume `just kubeconfig <cluster>` has been run and need the EKS proxy bypass — `NO_PROXY=...,.eks.amazonaws.com no_proxy=...,.eks.amazonaws.com` prefix, omitted below for readability.

**What version of module X is on this cluster?**
```bash
kubectl get cm osdc-deploy-module-finish-<module> -n osdc-system -o yaml
```
Read `.data.commit`, `.data.branch`, `.data.timestamp`, `.data.user`, `.data.status`.

**Does prod match staging for module X?**
```bash
just kubeconfig meta-prod-aws-ue1
PROD_COMMIT=$(kubectl get cm osdc-deploy-module-finish-karpenter -n osdc-system -o jsonpath='{.data.commit}')
just kubeconfig meta-staging-aws-uw1
STAGING_COMMIT=$(kubectl get cm osdc-deploy-module-finish-karpenter -n osdc-system -o jsonpath='{.data.commit}')
echo "prod=$PROD_COMMIT  staging=$STAGING_COMMIT"
git log --oneline "$PROD_COMMIT..$STAGING_COMMIT" -- modules/karpenter/
```

**Who deployed last on this cluster, and when?**
```bash
just deploy-status <cluster>          # color-coded; reads all CMs at once
```
Or raw, sorted by actual deploy time (NOT `creationTimestamp`):
```bash
kubectl get cm -n osdc-system -l app.kubernetes.io/managed-by=osdc-deploy-log \
    -o jsonpath='{range .items[*]}{.data.timestamp}{"\t"}{.metadata.name}{"\t"}{.data.user}{"\t"}{.data.status}{"\n"}{end}' \
    | sort | tail -20
```

**Why did the last deploy fail?**
```bash
just deploy-status <cluster>          # red "failed" rows identify the failing scope
# Or list every failure on the cluster:
kubectl get cm -n osdc-system -l app.kubernetes.io/managed-by=osdc-deploy-log -o json \
    | jq -r '.items[] | select(.data.status=="failed") | "\(.data.timestamp)\t\(.metadata.name)\t\(.data.commit)\t\(.data.user)"'
```
The ConfigMap only records THAT it failed, not WHY — pair this with the actual deploy logs (CI job, terminal scrollback). When `deploy-base` fails inside `just deploy`, you typically see TWO failed records (the inner `deploy-base` and the outer `deploy`).

**Is a deploy in progress right now?**
```bash
just deploy-status <cluster> | grep -i "in progress"
```
`deploy-status` flags "in progress" when the start ConfigMap's timestamp is newer than the finish one's. **There is no TTL or liveness check** — an orchestrator that was SIGKILL'd hours ago shows as in-progress forever until somebody re-runs the same scope/name. Sanity-check the start `timestamp`: anything >1h old with no recent module activity is almost certainly a dead orchestrator, not a live deploy.

**Timeline of every deploy event today, narrowed to one scope:**
```bash
just deploy-status <cluster> <name>   # name = "deploy" | "deploy-base" | "deploy-module-<MOD>" | "<MOD>"
```
The optional `<name>` filter switches the history table to include BOTH start and finish events (richer timeline) and bumps the row cap to 20. Without it, only finish events are shown, capped at 10.

**Full JSONL history for one record:**
```bash
kubectl get cm osdc-deploy-module-history-karpenter -n osdc-system -o jsonpath='{.data.entries}'
```

### Gotchas

- **`.metadata.creationTimestamp` is NOT the deploy time** — it is when the ConfigMap was first created (possibly weeks ago). `just deploy-history` sorts by `creationTimestamp` and is therefore mostly an inventory of which records exist, not a timeline. Always use `.data.timestamp` for actual deploy time.
- **"In progress" can be stale.** No TTL, no heartbeat. A re-run of the same scope/name is the only thing that clears it.
- **`user` is `$USER`, not the GH actor.** CI runs as whatever user the runner pod uses — usually meaningless for attribution. Cross-reference with the CI job URL.
- **`branch=detached` is normal in CI** (SHA checkouts). Not a bug.
- **Removed modules leak ConfigMaps.** Records for a module dropped from `clusters.yaml` stay in `osdc-system` until someone `kubectl delete cm`s them.
- **Audit writes are non-fatal and silent on failure.** If the audit `kubectl` call itself fails (network blip, RBAC), the deploy still proceeds and the audit record simply doesn't exist — only a `Warning: deploy-log failed …` on stderr.
- **Direct `./modules/foo/deploy.sh` invocations produce zero audit records.** Module scripts don't source `deploy-log.sh`; only the `just` recipes do.
- **History caps at 50 entries combined (start + finish)** → effectively the last ~25 deploys per record. High-churn clusters lose old history fast.

## tofu (OpenTofu) — read-only

**NEVER run `terraform` — this is OpenTofu-only.** Running `terraform` against the same state directory will corrupt state with no recovery. Use `tofu` or the `just` recipes (which call `tofu` internally) for everything.

Base infra terraform code lives in `modules/eks/terraform/` even though state is keyed under `${CLUSTER}/base/terraform.tfstate` (`base` here is the *state slot* name; the directory `base/terraform/` does not exist — the real code is in `modules/eks/terraform/`).

Each cluster's state lives in its own `state_bucket` (defined in `clusters.yaml`, e.g. `ciforge-tfstate-meta-staging-aws-uw1`, `ciforge-tfstate-arc-cbr-prod`, `ciforge-tfstate-arc-cbr-prod-ue1`, `ciforge-tfstate-arc-cbr-prod-uw1`). Lock table is shared: `ciforge-terraform-locks`. To inspect state for a specific cluster, first init with the cluster's backend config:

```bash
cd modules/eks/terraform
tofu init -reconfigure \
    -backend-config="bucket=ciforge-tfstate-meta-staging-aws-uw1" \
    -backend-config="key=meta-staging-aws-uw1/base/terraform.tfstate" \
    -backend-config="region=us-west-2" \
    -backend-config="dynamodb_table=ciforge-terraform-locks"
tofu show                    # Current state
tofu output                  # Output values
tofu state list              # All managed resources
tofu state show <addr>       # Inspect a single resource
```

For module state (karpenter, arc, monitoring, etc.) the pattern is the same but the code lives in `modules/<module>/terraform/` and the key is `<cluster>/<module>/terraform.tfstate`. The simplest way to plan-only a whole cluster is `just plan <cluster>` — it handles init + plan for base and every module in order.

**The state bucket and lock table always live in `us-west-2` regardless of the cluster's own workload region.** This is the hardcoded `STATE_REGION` constant in `scripts/state-config.sh` (the single source of truth) and is what every `justfile` recipe and module `deploy.sh` uses. `mise.toml` also exports `AWS_REGION=us-west-2` by default for the same reason. Don't substitute the cluster's region here, even for clusters running outside us-west-2.

**lf-* state buckets** (`lf-osdc-tfstate-prod-ue1`, `lf-osdc-tfstate-prod-ue2`) live in another AWS account and are not readable from this laptop — `tofu init` against them will fail on S3 auth.

## Boundaries: What NOT to Do with CLI

**This skill is READ-ONLY.** Everything below is forbidden without explicit user instruction — and even then, prefer surfacing the appropriate `just` recipe over running the raw command.

- **NEVER** run `terraform` (any subcommand, including `terraform init`/`plan`) — this project is OpenTofu-only and running `terraform` against the same directory will corrupt state with no recovery. Always use `tofu` or `just` recipes.
- **NEVER** `kubectl apply/create/delete/edit/patch/replace/scale/cordon/drain/taint/label/annotate` — use `just` recipes for deployments
- **NEVER** `kubectl exec` into a pod for anything other than running idempotent read-only diagnostic commands (`ls`, `cat`, `env`, `ps`, `netstat`, `nslookup`). No mutating commands, no shell sessions left running.
- **NEVER** `helm install/upgrade/uninstall/rollback` — use `just deploy-*` recipes
- **NEVER** `tofu apply/destroy/import/state rm/state mv/taint/untaint/refresh -auto-approve` — use `just deploy-*` recipes; `tofu show`/`output`/`state list`/`state show`/`plan` are the only safe verbs
- **NEVER** `aws` write operations (create-*, delete-*, modify-*, update-*, put-*, terminate-*, attach-*, detach-*, associate-*, disassociate-*, run-instances, etc.). Only `describe-*`, `list-*`, `get-*`, `head-*` verbs are safe.
- **NEVER** scale, cordon, drain, or taint nodes directly
- **NEVER** write to the project's git repo (no `git commit`, `git push`, `git merge`, `git rebase`, `git tag`, branch deletion). Read-only git commands are fine.
- **NEVER** edit kubeconfig by hand — always go through `just kubeconfig <cluster>` (which uses `scripts/kubeconfig-lock.sh` to serialize concurrent writes)

If you need to make a change, find or suggest the appropriate `just` recipe. Don't experiment with the cluster — even on staging.

## Node Compactor Debugging

```bash
# Controller logs
kubectl logs -n kube-system deploy/node-compactor -f

# Check taint state on nodes
kubectl get nodes -o custom-columns='NAME:.metadata.name,TAINTS:.spec.taints[*].key'
```

## NodeLocal DNSCache (NLD) Debugging

NLD is a per-node DaemonSet (`hostNetwork: true`, listens on `fd00::10` and the kube-dns Service ClusterIP). Pods continue resolving via the unchanged kube-dns ClusterIP — NLD intercepts via NOTRACK ip6tables (IPv6-only EKS) on a dummy `nodelocaldns` interface.

```bash
# DaemonSet health snapshot (DESIRED/CURRENT/READY should match node count)
kubectl get ds node-local-dns -n kube-system

# Per-node pod placement (verify one pod per node, identify any not-Running)
kubectl get pods -n kube-system -l k8s-app=node-local-dns -o wide

# Recent logs across all NLD pods
kubectl logs -n kube-system -l k8s-app=node-local-dns --tail=50

# Verify DNS resolution from a debug pod (both paths)
kubectl run dns-test --rm -it --image=busybox:1.36 --restart=Never -- \
    nslookup kubernetes.default.svc.cluster.local fd00::10
kubectl run dns-test --rm -it --image=busybox:1.36 --restart=Never -- \
    nslookup kubernetes.default.svc.cluster.local  # via kube-dns ClusterIP
```

**Container is a minimal image — `kubectl exec ... -- env` (or any shell/busybox cmd) does NOT work.** For runtime env var inspection use `/proc/1/environ` if accessible; otherwise rely on functional DNS queries as the authoritative test. Setup errors surface via the `coredns_nodecache_setup_errors_total` metric (note: NOT `nodelocaldns_setup_errors_total`).

## VPC CNI Custom Networking — ENIConfigs / base node label

**Legacy — inert under IPv6 mode.** VPC CNI Custom Networking is unsupported in IPv6-only EKS; the ENIConfig CRs and the `ipam.osdc.internal/eni-config` node labels are kept inert as a rollback fallback to IPv4 + Custom Networking. Under IPv6 the commands below still execute but the labels carry no behavioral meaning.

```bash
# List ENIConfig CRs (one per AZ for base nodes)
kubectl get eniconfigs.crd.k8s.amazonaws.com

# Verify base nodes carry the AZ-matched eni-config label
kubectl get nodes -l role=base-infrastructure \
    -o custom-columns='NAME:.metadata.name,ZONE:.metadata.labels.topology\.kubernetes\.io/zone,ENI:.metadata.labels.ipam\.osdc\.internal/eni-config'
```
