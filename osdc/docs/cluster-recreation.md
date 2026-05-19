# Cluster Recreation Runbook

## What this is and when to use it

Operator-facing runbook for destroying and recreating an existing OSDC cluster. Use this whenever a planned change touches a property of `aws_eks_cluster` (or one of its directly-dependent resources) that is **immutable after cluster creation** and that AWS rejects in-place with a ForceNew plan.

Common triggers:

- IP family change (`kubernetes_network_config.ip_family`: `ipv4` ↔ `ipv6`)
- Service CIDR change (`kubernetes_network_config.service_ipv4_cidr` / `service_ipv6_cidr`)
- KMS encryption added to a previously-unencrypted cluster (`encryption_config`)
- Any other field on `aws_eks_cluster` (or the VPC it lives in) that the AWS API refuses to mutate

Per-cluster: do `arc-staging` (us-west-1) first, then `arc-cbr-production` (us-east-2) once staging has soaked. Never pipeline both clusters.

**The code change that enables the new cluster shape MUST already be merged to `main` and CI must be green.** `just deploy <cluster>` after destroy is what brings up the new cluster — there is no manual override path.

Accepted data losses (default; audit per migration):

- **Harbor S3 bucket** (`<cluster_name>-harbor-registry`) is destroyed. All cached image layers are gone. Re-cached lazily from upstream over weeks as runners pull images.
- **EFS pypi-cache wheelhouse** is destroyed. Wheels rebuild from the upstream S3 wheel pipeline over days.
- Any other in-cluster state that is not preserved by terraform (cluster-local PVCs, in-cluster secrets created at runtime, ConfigMaps written by controllers, etc.).

**Operator action**: before scheduling the maintenance window, audit every module being destroyed for data the operator does not want to lose. Surface anything ambiguous to stakeholders. If anything unacceptable surfaces, design preservation steps and add them to this runbook (or a migration-specific addendum) BEFORE proceeding.

Out of scope for this runbook:

- The migration-specific code changes themselves — those must be merged before following this runbook
- Harbor S3 / EFS data preservation (design separately if the migration cannot tolerate the default loss)
- Dependency / tool / image version bumps

---

## Pre-flight checklist

### Pre-flight commands

Read-only sanity checks. Run these BEFORE destroying anything. If any do not match expectations, STOP.

```bash
# Verify AWS account
NO_PROXY="$NO_PROXY,.amazonaws.com" no_proxy="$no_proxy,.amazonaws.com" \
  aws sts get-caller-identity --query 'Account' --output text
# Expected: 308535385114

# Verify kubectl context (after just kubeconfig <cluster>)
NO_PROXY="$NO_PROXY,.eks.amazonaws.com" no_proxy="$no_proxy,.eks.amazonaws.com" \
  mise exec -- kubectl config current-context
# Expected: arn:aws:eks:<region>:308535385114:cluster/<cluster_name>

# Verify mise tools
mise doctor

# Verify just/uv/tofu versions
just --version
uv --version
tofu version
```

### Checklist

Complete every item before scheduling the maintenance window.

- [ ] All migration code merged to `main` and CI green (`just lint`, `just test`)
- [ ] Any out-of-band image / hook bumps required by the migration are deployed (handled outside this runbook)
- [ ] Grafana dashboards verified: cluster-name labels match what the recreated cluster will use (`pytorch-arc-staging`, `pytorch-arc-cbr-production`) so panels stay continuous across recreation
- [ ] AWS quotas in the target region:
  - `L-FE5A380F` (NAT GWs / AZ): ≥ 1 per AZ (current default of 5 is plenty)
  - Any AWS quotas the migration-specific changes introduce — operator must enumerate per migration
- [ ] Data audit complete (see "Accepted data losses" above) — preservation steps in place for anything not safe to lose
- [ ] Maintenance window communicated to pytorch/pytorch on-call and any stakeholders consuming the cluster
- [ ] On-call reviewer paired for the cutover (2 people on the bridge)
- [ ] State-bucket backup directory created locally: `mkdir -p ${HOME}/.osdc-cutover/${CLUSTER}`

---

## Per-cluster sequence

Run the steps below per cluster, in order. Do NOT pipeline staging and production — staging must complete its soak before production starts.

> **Shell prerequisite — set `CLUSTER` once, up front.** Every code block below references `${CLUSTER}` (e.g. `uv run scripts/cluster-config.py ${CLUSTER} cluster_name`, `just deploy ${CLUSTER}`, S3 keys under `s3://$(uv run scripts/cluster-config.py ${CLUSTER} state_bucket)/...`). If it's unset, commands either no-op confusingly or — worse — target the wrong bucket/region. Export it at the start of the session and keep working in the same shell.
>
> Also source `scripts/state-config.sh` so `STATE_REGION` is in scope for the state-bucket and DynamoDB commands below.
>
> ```bash
> export CLUSTER=arc-staging          # or arc-cbr-production
> source scripts/state-config.sh      # exports STATE_REGION
> ```
>
> If you open a new shell mid-cutover, re-export it before resuming.

### 1. Disable OSDC traffic

Flip the OSDC experiment / GK that gates user traffic into the cluster. New jobs stop being routed; in-flight jobs continue until drained in step 2.

### 2. Drain runners

```bash
just drain-runners ${CLUSTER}
```

This patches every `AutoScalingRunnerSet` to `maxRunners=0`, taints runner nodes, and polls until pod count is zero or the timeout (default 1h) hits.

For stragglers: operator decides whether to wait (re-run with `OSDC_DRAIN_TIMEOUT_SECS=<larger>`) or force-terminate via `kubectl delete pod -n arc-runners --grace-period=0 --force <pod>`. Force-termination leaks job state at the GitHub side — coordinate with pytorch/pytorch on-call.

Verify:

```bash
kubectl get pods -n arc-runners -l app.kubernetes.io/component=runner --no-headers | wc -l   # must be 0
```

Once runner pods are gone, tear down Karpenter-managed compute so the destroy doesn't stall on lingering EC2.

**Order matters.** Delete NodePools FIRST — Karpenter cascade-deletes the owned NodeClaims and gracefully terminates the EC2 instances via its `karpenter.sh/termination` finalizer. Deleting NodeClaims first leaves orphans Karpenter cannot reconcile (see Recovery below).

```bash
kubectl delete nodepools --all
```

This also stops re-provisioning: `drain-runners` only zeroes out ARC `AutoScalingRunnerSet` capacity, but non-ARC workloads (buildkit, pypi-cache, monitoring DaemonSets) keep requesting nodes. With no NodePool spec left, Karpenter has nothing to provision from. Buildkit / pypi-cache pods sit Pending until `tofu destroy` removes their controllers — that's fine.

Wait for NodeClaims to drain:

```bash
kubectl get nodeclaims    # expect: No resources found
```

If they hang in `Terminating` (status: `Ready=False`, non-empty `metadata.deletionTimestamp`), see Recovery below.

#### Recovery: stuck NodeClaim finalizers

Symptom: `kubectl get nodeclaims` keeps showing the same NodeClaims with the same names, and `kubectl delete --wait=false` "deletes" them but they reappear. Karpenter controller logs spam `NodePool.karpenter.sh "<name>" not found (nodeclaim=<x>, nodepool=<name>)`. The `karpenter.sh/termination` finalizer never clears because the controller's reconciler errors out before it gets to the finalizer-removal step.

Fix: terminate the orphan EC2 instances directly, then force-clear the finalizers.

```bash
# 1) Find Karpenter-managed instances for this cluster.
#    Tag for cluster scoping is `eks:eks-cluster-name` (NOT `karpenter.sh/cluster`,
#    which doesn't exist).
CNAME=$(uv run scripts/cluster-config.py ${CLUSTER} cluster_name)
REGION=$(uv run scripts/cluster-config.py ${CLUSTER} region)
INSTANCES=$(NO_PROXY="$NO_PROXY,.amazonaws.com" no_proxy="$no_proxy,.amazonaws.com" \
  aws ec2 describe-instances --region "${REGION}" \
    --filters "Name=tag-key,Values=karpenter.sh/nodepool" \
              "Name=tag:eks:eks-cluster-name,Values=${CNAME}" \
              "Name=instance-state-name,Values=running,pending,stopping,stopped" \
    --query 'Reservations[].Instances[].InstanceId' --output text)
echo "$INSTANCES"

# 2) Terminate them. Without this the VPC destroy in step 6 will fail
#    because the instances still hold ENIs in the soon-to-be-destroyed subnets.
[[ -n "$INSTANCES" ]] && NO_PROXY="$NO_PROXY,.amazonaws.com" no_proxy="$no_proxy,.amazonaws.com" \
  aws ec2 terminate-instances --region "${REGION}" --instance-ids $INSTANCES

# 3) Force-strip the finalizers so the k8s objects clean up.
kubectl get nodeclaims -o name | xargs -r -I{} \
  kubectl patch {} --type=merge -p '{"metadata":{"finalizers":null}}'
```

Steps 1+2 are the load-bearing ones — they kill the actual AWS resources. Step 3 just unblocks the k8s objects so `kubectl get nodeclaims` returns empty.

### 3. Capture pre-cutover state

```bash
export STATE_DIR=${HOME}/.osdc-cutover/${CLUSTER}
mkdir -p ${STATE_DIR}

BUCKET=$(uv run scripts/cluster-config.py ${CLUSTER} state_bucket)

# Tofu state for every module (in case of partial-recovery debugging)
for mod in $(uv run scripts/cluster-config.py ${CLUSTER} modules) base; do
  KEY="${CLUSTER}/${mod}/terraform.tfstate"
  NO_PROXY="$NO_PROXY,.amazonaws.com" no_proxy="$no_proxy,.amazonaws.com" \
    aws s3 cp "s3://${BUCKET}/${KEY}" \
      "${STATE_DIR}/${mod}.tfstate" --region ${STATE_REGION} || true
done

# ConfigMap snapshot (deploy history, harbor secrets, etc.)
kubectl get cm -A -o yaml > ${STATE_DIR}/configmaps-pre-cutover.yaml

# CronJob suspend state (so step 9 only un-suspends entries originally false)
kubectl get cronjobs -A -o json \
  | jq -r '.items[] | "\(.metadata.namespace)/\(.metadata.name) \(.spec.suspend // false)"' \
  > ${STATE_DIR}/cronjob-state-pre-cutover.txt

# Grafana baseline metrics
# Capture the dashboard URLs you want to compare post-cutover. Suggested:
#   - pod startup time P50/P99
#   - kube-apiserver p99 latency
#   - CoreDNS QPS, node-local DNS health
#   - NAT GW egress bytes
#   - Any migration-specific panels
echo "Capture baseline panel screenshots / CSVs from Grafana now." >&2
```

Treat `${STATE_DIR}` as sensitive — do not commit, keep on operator workstation.

### 4. Suspend CronJobs

```bash
kubectl get cronjobs -A -o json \
  | jq -r '.items[] | select(.spec.suspend != true) |
           "kubectl -n \(.metadata.namespace) patch cronjob \(.metadata.name) -p '\''{\"spec\":{\"suspend\":true}}'\''"' \
  | bash
```

This prevents scheduled work (zombie-cleanup, harbor-cache-recovery, image-cache-janitor) from racing the destroy.

### 5. Empty Harbor S3 bucket

`aws_s3_bucket.harbor_registry` is declared **without `force_destroy = true`** — `tofu destroy` will refuse on a non-empty bucket. Drain the bucket first:

```bash
CNAME=$(uv run scripts/cluster-config.py ${CLUSTER} cluster_name)
REGION=$(uv run scripts/cluster-config.py ${CLUSTER} region)
NO_PROXY="$NO_PROXY,.amazonaws.com" no_proxy="$no_proxy,.amazonaws.com" \
  aws s3 rm "s3://${CNAME}-harbor-registry" --recursive --region ${REGION}

# If versioning is on, also delete versions + delete-markers (paginated)
NO_PROXY="$NO_PROXY,.amazonaws.com" no_proxy="$no_proxy,.amazonaws.com" \
  aws s3api list-object-versions --bucket ${CNAME}-harbor-registry --region ${REGION} \
    --output json | jq -r '
      [.Versions // [], .DeleteMarkers // []] | flatten |
      .[] | "\(.Key)\t\(.VersionId)"' \
  | while IFS=$'\t' read -r k v; do
      aws s3api delete-object --bucket ${CNAME}-harbor-registry --key "$k" --version-id "$v" --region ${REGION}
    done
```

The pypi-cache module uses EFS (not S3), so no equivalent step is needed there — `aws_efs_file_system` destroys cleanly even when populated.

### 6. Tofu destroy modules in dependency order

#### Recommended: run the helper script

```bash
./scripts/destroy-cluster.sh ${CLUSTER}
```

The script encodes the dependency order, picks the right `-var` set per module (modules need `cluster_name` / `aws_region` / `state_bucket` / `cluster_id`; the base needs the full `tfvars` string from `scripts/cluster-config.py ${CLUSTER} tfvars`), uses the cluster's actual `state_bucket` (production is `ciforge-tfstate-arc-cbr-prod`, not `…-arc-cbr-production`), and gates the base destroy behind a second confirmation prompt. It does NOT empty the Harbor S3 bucket — step 5 must be run first.

Env-var overrides:

- `OSDC_CONFIRM=yes` — skip the up-front "type the cluster name" prompt
- `OSDC_CONFIRM_BASE=destroy` — skip the final "type 'destroy <cluster>'" prompt before the base destroy

Only modules that have a `terraform/main.tf` (`karpenter`, `pypi-cache`, and the base `modules/eks`) are tofu-destroyed. Every other module is k8s-only and dies with the EKS cluster — the script lists them under "k8s-only modules" but does not act on them.

If the script fails partway through, fix the underlying issue and re-run — `tofu destroy` is idempotent against already-destroyed state.

#### Manual procedure (use only when bypassing the script)

Destroy in **reverse-deploy order**: leaf modules first, base last. `tofu init -reconfigure` in each module directory before `destroy`; pass the same `-var` flags the deploy uses.

Compute the base tfvars string once:

```bash
TFVARS=$(uv run scripts/cluster-config.py ${CLUSTER} tfvars)
BUCKET=$(uv run scripts/cluster-config.py ${CLUSTER} state_bucket)
CNAME=$(uv run scripts/cluster-config.py ${CLUSTER} cluster_name)
REGION=$(uv run scripts/cluster-config.py ${CLUSTER} region)
```

`TFVARS` matches the **base** schema only — module destroys reject the extra vars; pass just `-var="cluster_name=${CNAME}" -var="aws_region=${REGION}" -var="state_bucket=${BUCKET}" -var="cluster_id=${CLUSTER}"` for those.

Destroy order (reverse of the cluster's `modules` list in `clusters.yaml`, ending at the base):

1. **Leaf cron / enforcement modules** (no dependents):
   - `modules/harbor-cache-recovery`
   - `modules/zombie-cleanup`
   - `modules/cache-enforcer`
2. **Observability**:
   - `modules/logging`
   - `modules/monitoring`
3. **Workload modules** (depend on arc / nodepools):
   - `modules/buildkit`
   - `modules/arc-runners-h100` (production only)
   - `modules/arc-runners-b200` (production only)
   - `modules/arc-runners`
   - `modules/arc`
4. **Compute provisioning**:
   - `modules/nodepools-h100` (production only)
   - `modules/nodepools-b200` (production only)
   - `modules/nodepools`
   - `modules/karpenter`
5. **PyPI cache** — **WARNING**: EFS file system + wheelhouse data is destroyed.
   - `modules/pypi-cache`
6. **Base / EKS** — **WARNING**: Harbor S3 bucket (already emptied in step 5), VPC, EKS control plane, base node group, IAM roles, KMS keys all destroyed.
   - `modules/eks` (state key `${CLUSTER}/base/terraform.tfstate`)

Per-module command pattern:

```bash
cd modules/<mod>/terraform
NO_PROXY="$NO_PROXY,.amazonaws.com" no_proxy="$no_proxy,.amazonaws.com" \
  tofu init -reconfigure \
    -backend-config="bucket=${BUCKET}" \
    -backend-config="key=${CLUSTER}/<mod>/terraform.tfstate" \
    -backend-config="region=${STATE_REGION}" \
    -backend-config="dynamodb_table=ciforge-terraform-locks"
NO_PROXY="$NO_PROXY,.amazonaws.com" no_proxy="$no_proxy,.amazonaws.com" \
  tofu destroy -auto-approve \
    -var="cluster_name=${CNAME}" \
    -var="aws_region=${REGION}" \
    -var="state_bucket=${BUCKET}" \
    -var="cluster_id=${CLUSTER}"
cd -
```

For the base, the key is `${CLUSTER}/base/terraform.tfstate` (not `${CLUSTER}/eks/...`), and the destroy must use the full base var set:

```bash
cd modules/eks/terraform
NO_PROXY="$NO_PROXY,.amazonaws.com" no_proxy="$no_proxy,.amazonaws.com" \
  tofu init -reconfigure \
    -backend-config="bucket=${BUCKET}" \
    -backend-config="key=${CLUSTER}/base/terraform.tfstate" \
    -backend-config="region=${STATE_REGION}" \
    -backend-config="dynamodb_table=ciforge-terraform-locks"
NO_PROXY="$NO_PROXY,.amazonaws.com" no_proxy="$no_proxy,.amazonaws.com" \
  eval tofu destroy ${TFVARS} -auto-approve
cd -
```

Hangs to expect:

- `aws_eks_cluster` destroy can stall ~10 min while EKS deletes the control plane
- `aws_nat_gateway` destroy waits ~5 min for the NAT to drain
- `aws_efs_file_system` destroy fails if mount targets aren't gone — `tofu destroy` deletes them in the same plan, but a half-applied destroy may need a retry
- `aws_eks_addon.vpc_cni` may stall if it can't reach the API server because the cluster is half-gone — re-run destroy

### 7. Verify survivors

After base destroy completes, spot-check that NON-cluster resources are untouched:

```bash
# ECR mirrors (separate from Harbor S3 — these survive)
NO_PROXY="$NO_PROXY,.amazonaws.com" no_proxy="$no_proxy,.amazonaws.com" \
  aws ecr describe-repositories --region ${REGION} | jq '.repositories | length'

# Tofu state bucket (lives in us-west-2 regardless of cluster region)
NO_PROXY="$NO_PROXY,.amazonaws.com" no_proxy="$no_proxy,.amazonaws.com" \
  aws s3 ls "s3://${BUCKET}/" --region ${STATE_REGION}

# DynamoDB lock table (shared across all clusters)
NO_PROXY="$NO_PROXY,.amazonaws.com" no_proxy="$no_proxy,.amazonaws.com" \
  aws dynamodb describe-table --table-name ciforge-terraform-locks --region ${STATE_REGION} \
    --query 'Table.TableStatus'

# S3 wheel pipeline bucket (external to OSDC, feeds pypi-cache)
NO_PROXY="$NO_PROXY,.amazonaws.com" no_proxy="$no_proxy,.amazonaws.com" \
  aws s3 ls "s3://pytorch-pypi-wheel-cache/" --region us-east-2 | head -5

# Grafana Cloud — verify dashboards still respond (browser check)
```

If anything cluster-related is left over (orphaned ENIs, NAT GW, EIP, security groups stuck on `cluster_security_group`), clean up by hand before redeploying — leftovers can collide with the recreated cluster.

### 8. Bootstrap state (only if state bucket was destroyed) and redeploy

The state bucket persists across recreation because it is bootstrapped via `scripts/bootstrap-state.sh` (not part of `modules/eks`). Skip the bootstrap step unless you also nuked the state bucket on purpose. If you did:

```bash
just bootstrap ${CLUSTER}
```

Then full deploy:

```bash
just deploy ${CLUSTER}
```

This applies base (VPC, EKS, Harbor S3/IAM, base k8s resources), then every module in the order defined in `clusters.yaml`.

### 9. Smoke tests

```bash
just smoke ${CLUSTER}
```

Runs the per-base and per-module smoke suite under `modules/eks/tests/smoke/` and each module's `tests/smoke/`. All must pass before resuming traffic. Migrations that introduce new invariants should add assertions to the relevant smoke test before merging — those assertions then become a deploy-time gate here.

### 10. Restore CronJobs

Only un-suspend CronJobs that were `false` originally (preserves any CronJob suspended for unrelated reasons):

```bash
awk '$2=="false"{print $1}' ${STATE_DIR}/cronjob-state-pre-cutover.txt \
  | while IFS=/ read -r ns name; do
      kubectl -n "$ns" patch cronjob "$name" -p '{"spec":{"suspend":false}}'
    done
```

### 11. Re-enable runner traffic

`just deploy` already re-synced every `AutoScalingRunnerSet` from `modules/arc-runners/defs/`, so `maxRunners` is back at the def value. Re-enable the OSDC experiment / GK and watch the queue drain.

`just resume-runners` is NOT used in standard cutover — it's out-of-band recovery only.

---

## Validation gates

Per cluster, all gates must pass within the maintenance window before declaring cutover complete and starting the soak.

| Gate | Check | Pass criterion |
|---|---|---|
| Cluster up | `kubectl get nodes -o wide` | All `Ready`; node count matches `base_node_count` for the cluster |
| Pod-to-internet egress | `kubectl run -it --rm --image=curlimages/curl curl-test -- curl -sI https://github.com/` | HTTP 200 |
| Harbor proxy | `kubectl run -it --rm --image=<harbor>/dockerhub-proxy/library/alpine:latest pull-test -- echo ok` | Image pulls — proxy fetches from upstream (cold cache) |
| pypi-cache | `kubectl exec <runner-class pod> -- pip install --index-url http://pypi-cache.pypi-cache.svc/simple/cuda-12.6.3/ requests` | Wheel fetched via pypi-cache (proxy-fetches from upstream on miss) |
| Distributed test (production only) | NCCL / `torch.distributed` 2-node smoke test on H100 or B200 fleet | Job completes; no socket errors |
| Smoke suite | `just smoke ${CLUSTER}` | Exit 0 |
| Migration-specific gates | Operator adds checks specific to the migration (e.g., new IP family addressing, new addon behavior, changed network policy semantics, KMS-backed secret round-trip) | Per-migration |

If any gate fails: stop, do not advance to soak. Investigate; either fix in place or roll back per the rollback section.

### IRSA refresh check

Cluster recreation produces a new OIDC issuer URL. All IRSA-using ServiceAccounts get re-bound automatically by terraform on `just deploy` (modules read OIDC ARN from `terraform_remote_state.base.outputs.oidc_provider_arn`). Verify post-deploy:

```bash
NO_PROXY="$NO_PROXY,.eks.amazonaws.com" no_proxy="$no_proxy,.eks.amazonaws.com" \
  mise exec -- kubectl get sa -A -o json | \
  jq -r '.items[] | select(.metadata.annotations."eks.amazonaws.com/role-arn") | "\(.metadata.namespace)/\(.metadata.name) \(.metadata.annotations."eks.amazonaws.com/role-arn")"'
# Spot-check that role ARNs reference roles bound to the NEW OIDC provider URL
```

---

## Soak window

Run for **≥ 3 days, preferably 7 days at production load** before declaring success and moving to the next cluster.

Watch list (Grafana panels, alerts, and ad-hoc kubectl):

- **NAT GW data egress trend** — compare bytes-out vs the pre-cutover baseline. Spike > 2x baseline indicates an unexpected destination set; investigate via VPC flow logs.
- **conntrack table fill** — `node_nf_conntrack_entries / node_nf_conntrack_entries_limit` per node. > 80% fill is concerning; > 95% will start dropping connections (`nf_conntrack: table full, dropping packet`).
- **kube-apiserver p99 latency** — should match pre-cutover baseline. Drift > 2x is a red flag (etcd / control plane sizing issue).
- **CoreDNS QPS** — should be flat vs baseline. Spike means node-local DNS is missing.
- **Node-local DNS health** — `coredns_nodecache_setup_errors_total` must remain 0. Any non-zero value means NLD failed to set up its iptables intercept on a node; the dummy interface bind will still work but liveness can flap.
- **Pod startup time P50 / P99** — vs pre-cutover baseline. Drift > 2x P99 needs investigation (likely VPC CNI prefix warm-pool tuning or scheduler pressure).
- **GitHub Actions runner success rate** — pull from ClickHouse / pytorch HUD. Should match pre-cutover within ± 1 percentage point.
- **Harbor 5xx rate / cache miss rate** — expected to be elevated for the first ~ 2 weeks as the cold S3 backend re-fills. Watch for 5xx spikes that are NOT cache misses.
- **pypi-cache cold-start errors** — wheel fetches from upstream may temporarily exceed the network policy budget; tune as needed. Re-population from the wheel-pipeline S3 takes days.
- **Migration-specific watch items** — operator enumerates per migration (new metrics introduced, behaviors the migration changes, regressions specific to the property being mutated).

Daily check-in: read the watch list, decide go/no-go for the next 24h. Promote `arc-staging` → `arc-cbr-production` only when staging has been clean for the full soak window.

---

## Rollback

Cluster recreation is one-way. Once destroyed, you commit to recreate — there is no per-step rollback within a single cutover.

"Rollback" means: revert the migration code change on a hotfix branch, get it merged to `main`, then destroy + recreate the cluster a second time (same procedure as this runbook). Costs:

- One additional maintenance window per cluster
- Harbor S3 / EFS / any other accepted-loss data is lost a second time (rebuilds again over weeks)
- IRSA refresh again
- Any state you captured in step 3 is now two cutovers stale

Some migrations may design an explicit safety net (e.g., keeping inert fallback infrastructure in the codebase that can be activated post-recreation without another destroy). If the migration provides one, document the activation procedure in a migration-specific addendum to this runbook. For migrations without a safety net, plan one maintenance window per cluster for the rollback.

If the validation gates fail and a hotfix is small (parameter tweak, addon version), prefer fixing forward over rolling back — a forward fix is `just deploy ${CLUSTER}` after the hotfix is merged, no destroy needed.

---

## Post-cutover follow-ups

After both clusters are stable for several weeks:

- **Per-cluster tuning** — update `clusters.yaml` if the migration introduces any per-cluster overrides (e.g. component memory, replica counts).
- **Dashboard fixes** — fix any panels that hardcoded patterns affected by the migration (e.g. IP regexes, addon names, label keys). Use ClickHouse / Loki / Mimir to find dashboards still referencing the old patterns.
- **Monitoring / alerting additions** — consider whether the migration introduces new failure modes worth alerting on. Add the rules with the migration's PR if possible, or as a follow-up PR.
- **Codebase cleanup** — if the migration left behind transitional code (feature flags, dual-path conditionals, fallback infrastructure), schedule its removal once the new path has been stable long enough that rollback is no longer plausible. Treat as a separate PR.

---

## References

- `docs/architecture.md` — cluster architecture, base vs modules, deploy flow
- `docs/operations.md` — day-to-day ops; bootstrap and deploy recipes
- `scripts/destroy-cluster.sh` — the destroy helper invoked in step 6
- `scripts/cluster-config.py` — resolves cluster-name / region / state-bucket / tfvars / module list from `clusters.yaml`
- `clusters.yaml` — per-cluster module list and overrides (source of truth for destroy order)
- `modules/eks/tests/smoke/` — base smoke assertions; add migration-specific assertions here
