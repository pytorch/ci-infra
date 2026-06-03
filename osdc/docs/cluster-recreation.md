# Cluster Recreation Runbook

## 1. Introduction and pre-flight

Operator-facing runbook for destroying and recreating an existing OSDC cluster. Use this whenever a planned change touches a property of `aws_eks_cluster` (or a directly-dependent resource) that AWS rejects in-place with a ForceNew plan — VPC / IP family / CIDR changes, encryption_config additions, or any other immutable field.

**Per-cluster, never in parallel.** Do `arc-staging` (us-west-1) first; only promote to `arc-cbr-production` (us-east-2) after staging has soaked.

**Code change must already be on `main` with green CI.** `just deploy <cluster>` after destroy is the only path that brings up the new cluster shape.

**Accepted data losses** (audit and design preservation steps per change if unacceptable):

- Harbor S3 bucket (`<cluster_name>-harbor-registry`) — re-caches lazily over weeks.
- EFS pypi-cache wheelhouse — rebuilds from upstream S3 over days.
- Any in-cluster state not preserved by terraform (runtime-created PVCs / secrets / ConfigMaps).

### Pre-flight checklist

- [ ] Change code merged to `main`; `just lint` and `just test` green.
- [ ] Any out-of-band image / hook bumps required by the change are deployed.
- [ ] AWS quotas verified for the target region (NAT GWs / AZ ≥ 1; plus anything the change introduces).
- [ ] Maintenance window communicated to pytorch/pytorch on-call.
- [ ] Two operators on the bridge.
- [ ] Shell prepped: `export CLUSTER=<cluster>; source scripts/state-config.sh`.

---

## 2. Cluster destroy

### 1. Disable OSDC traffic

Flip the OSDC experiment / GK. New jobs stop being routed; in-flight jobs continue.

### 2. Drain runners and tear down compute

```bash
just drain-runners ${CLUSTER}
```

Patches every `AutoScalingRunnerSet` to `maxRunners=0`, taints runner nodes, polls until pod count is zero. For stragglers: `OSDC_DRAIN_TIMEOUT_SECS=<larger>` or `kubectl delete pod -n arc-runners --grace-period=0 --force <pod>` (the latter leaks GitHub-side job state — coordinate with on-call).

Then delete NodePools so Karpenter cascade-terminates owned EC2:

```bash
just kubeconfig ${CLUSTER}
kubectl get pods -n arc-runners -l app.kubernetes.io/component=runner --no-headers | wc -l   # must be 0
kubectl delete nodepools --all
kubectl get nodeclaims    # expect: No resources found
```

**Delete NodePools first, not NodeClaims** — Karpenter cascade-deletes the owned NodeClaims via its `karpenter.sh/termination` finalizer. Deleting NodeClaims first leaves orphans (see "Stuck NodeClaim finalizers" in the annex).

### 3. Suspend CronJobs

Snapshot suspend-state first so step 11 only re-enables what was originally active:

```bash
just kubeconfig ${CLUSTER}
mkdir -p ${HOME}/.osdc-cutover/${CLUSTER}
kubectl get cronjobs -A -o json \
  | jq -r '.items[] | "\(.metadata.namespace)/\(.metadata.name) \(.spec.suspend // false)"' \
  > ${HOME}/.osdc-cutover/${CLUSTER}/cronjob-state-pre-cutover.txt

kubectl get cronjobs -A -o json \
  | jq -r '.items[] | select(.spec.suspend != true) |
           "kubectl -n \(.metadata.namespace) patch cronjob \(.metadata.name) -p '\''{\"spec\":{\"suspend\":true}}'\''"' \
  | bash
```

Prevents scheduled work (zombie-cleanup, harbor-cache-recovery, image-cache-janitor) from racing the destroy.

### 4. Empty Harbor S3 bucket

`aws_s3_bucket.harbor_registry` lacks `force_destroy = true`; `tofu destroy` refuses on a non-empty bucket.

```bash
CNAME=$(uv run scripts/cluster-config.py ${CLUSTER} cluster_name)
REGION=$(uv run scripts/cluster-config.py ${CLUSTER} region)
NO_PROXY="$NO_PROXY,.amazonaws.com" no_proxy="$no_proxy,.amazonaws.com" \
  aws s3 rm "s3://${CNAME}-harbor-registry" --recursive --region ${REGION}

# Versioned bucket: also delete versions and delete-markers
NO_PROXY="$NO_PROXY,.amazonaws.com" no_proxy="$no_proxy,.amazonaws.com" \
  aws s3api list-object-versions --bucket ${CNAME}-harbor-registry --region ${REGION} --output json \
  | jq -r '[.Versions // [], .DeleteMarkers // []] | flatten | .[] | "\(.Key)\t\(.VersionId)"' \
  | while IFS=$'\t' read -r k v; do
      aws s3api delete-object --bucket ${CNAME}-harbor-registry --key "$k" --version-id "$v" --region ${REGION}
    done
```

EFS pypi-cache destroys cleanly even when populated; no equivalent step needed.

### 5. Tofu destroy

```bash
./scripts/destroy-cluster.sh ${CLUSTER}
```

Encodes the dependency order (leaf modules → workloads → compute → pypi-cache → base), picks per-module vs base var sets, gates the base destroy behind a confirmation prompt. Idempotent — re-run if it fails partway. Override prompts with `OSDC_CONFIRM=yes` and `OSDC_CONFIRM_BASE=destroy`.

Expected hangs: EKS control plane delete (~10 min), NAT GW drain (~5 min). If `aws_eks_addon.vpc_cni` stalls because the API server is half-gone, re-run.

Read `scripts/destroy-cluster.sh` if you need to bypass it (debugging partial state, etc.); the per-module command pattern lives there.

---

## 3. Cluster create

### 7. Set `pause_runners` (local edit, do NOT commit)

Add `pause_runners: true` at the top level of the cluster's entry in `clusters.yaml`:

```diff
 arc-staging:
   region: us-west-1
   cluster_name: arc-staging
+  pause_runners: true
   modules:
     - karpenter
```

`generate_runners.py` reads this and forces every rendered `AutoScalingRunnerSet` to `maxRunners: 0`, so the GitHub listener cannot request a runner even if traffic gets flipped on early.

**Keep this edit local through the entire window.** If it gets committed and merged, the next CI deploy rolls `pause_runners: true` to every cluster that change touches — blast radius is every prod cluster picking up the merged config. Stash it or work from a dirty tree; revert (`git checkout clusters.yaml`) before any unrelated commit.

### 8. Bootstrap state (only if state bucket was also destroyed)

```bash
just bootstrap ${CLUSTER}
```

Skip unless you nuked the state bucket on purpose — it persists across recreation.

### 9. Deploy from local

```bash
just deploy ${CLUSTER}
```

Run from local, NOT CI — CI deploys only what's on `main`, and the pause_runners edit is local-only. Applies base + every module in `clusters.yaml` order; arc-runners modules render with `maxRunners: 0` due to the pause flag.

### 10. Smoke gate (MANDATORY)

```bash
just smoke ${CLUSTER}
```

Must exit 0 before proceeding. Aggregates pytest from `modules/eks/tests/smoke/` and every enabled module's `tests/smoke/`; fetches kubeconfig itself.

**If smoke fails: do NOT re-enable traffic, do NOT revert pause_runners.** Fix in place (re-run `just deploy ${CLUSTER}`) or roll back per the rollback section.

Changes that introduce new invariants should add assertions to the relevant smoke test before merging.

### 11. Restore CronJobs

```bash
just kubeconfig ${CLUSTER}
awk '$2=="false"{print $1}' ${HOME}/.osdc-cutover/${CLUSTER}/cronjob-state-pre-cutover.txt \
  | while IFS=/ read -r ns name; do
      kubectl -n "$ns" patch cronjob "$name" -p '{"spec":{"suspend":false}}'
    done
```

### 12. Un-pause runners

**Staging** — local:

1. `git checkout clusters.yaml` to revert the pause edit.
2. `just deploy ${CLUSTER}` again.

**Production** — via CI:

1. Revert the local pause edit (it must never be merged).
2. Trigger `osdc-deploy-prod.yml` (`workflow_dispatch`) with `target` set to the specific cluster — NOT `all`.

The prod path is gated by the `osdc-production` GitHub environment and an OIDC trust-policy `sub` claim; CI is the only audit-clean way in.

### 13. Re-enable OSDC traffic

The un-pause deploy in step 12 already re-synced every `AutoScalingRunnerSet`. Flip the OSDC experiment / GK and watch the queue drain.

`just resume-runners` is out-of-band recovery, NOT the standard cutover path.

---

## 4. Annex and additional details

### Stuck NodeClaim finalizers

Symptom: NodeClaims reappear after delete; Karpenter logs spam `NodePool.karpenter.sh "<name>" not found`. The `karpenter.sh/termination` finalizer never clears because the reconciler errors before reaching the removal step.

Fix: terminate the EC2 instances directly, then strip the finalizers.

```bash
CNAME=$(uv run scripts/cluster-config.py ${CLUSTER} cluster_name)
REGION=$(uv run scripts/cluster-config.py ${CLUSTER} region)
INSTANCES=$(NO_PROXY="$NO_PROXY,.amazonaws.com" no_proxy="$no_proxy,.amazonaws.com" \
  aws ec2 describe-instances --region "${REGION}" \
    --filters "Name=tag-key,Values=karpenter.sh/nodepool" \
              "Name=tag:eks:eks-cluster-name,Values=${CNAME}" \
              "Name=instance-state-name,Values=running,pending,stopping,stopped" \
    --query 'Reservations[].Instances[].InstanceId' --output text)

[[ -n "$INSTANCES" ]] && NO_PROXY="$NO_PROXY,.amazonaws.com" no_proxy="$no_proxy,.amazonaws.com" \
  aws ec2 terminate-instances --region "${REGION}" --instance-ids $INSTANCES

just kubeconfig ${CLUSTER}
kubectl get nodeclaims -o name | xargs -r -I{} \
  kubectl patch {} --type=merge -p '{"metadata":{"finalizers":null}}'
```

The terminate-instances call is the load-bearing part; the finalizer strip just unblocks the k8s side. The cluster-scoping tag is `eks:eks-cluster-name`, not `karpenter.sh/cluster` (which doesn't exist).

### Validation gates

All must pass before declaring cutover complete and starting the soak.

| Gate | Check | Pass |
|---|---|---|
| Cluster up | `kubectl get nodes -o wide` | All `Ready`; count matches `base_node_count` |
| Pod egress | `kubectl run -it --rm --image=curlimages/curl curl-test -- curl -sI https://github.com/` | HTTP 200 |
| Harbor proxy | Pull `<harbor>/dockerhub-proxy/library/alpine:latest` | Image pulls (cold cache) |
| pypi-cache | `pip install --index-url http://pypi-cache.pypi-cache.svc/simple/cuda-12.6.3/ requests` | Wheel fetched |
| Distributed (prod only) | NCCL / `torch.distributed` 2-node on H100 or B200 | Job completes; no socket errors |
| Smoke | `just smoke ${CLUSTER}` | Exit 0 |
| Change-specific | Operator adds checks specific to the change | Per-change |

### IRSA refresh sanity check

Recreation produces a new OIDC issuer URL; modules pick it up automatically from `terraform_remote_state.base.outputs.oidc_provider_arn`. Spot-check that ServiceAccount role ARNs reference roles bound to the new OIDC provider:

```bash
just kubeconfig ${CLUSTER}
kubectl get sa -A -o json | \
  jq -r '.items[] | select(.metadata.annotations."eks.amazonaws.com/role-arn") |
         "\(.metadata.namespace)/\(.metadata.name) \(.metadata.annotations."eks.amazonaws.com/role-arn")"'
```

### Soak window

Run ≥ 3 days, preferably 7 days at production load, before promoting `arc-staging` → `arc-cbr-production`.

Watch:

- kube-apiserver p99 latency — drift > 2x baseline = etcd / control-plane sizing issue.
- CoreDNS QPS — flat vs baseline.
- Pod startup P50 / P99 — drift > 2x P99 = scheduler pressure or CNI tuning.
- GitHub Actions runner success rate — within ± 1pp of pre-cutover.
- Harbor 5xx / cache miss — elevated for ~ 2 weeks as cold S3 re-fills; watch for 5xx that are NOT misses.
- pypi-cache cold-start errors — re-population from wheel-pipeline S3 takes days.
- Change-specific watch items — operator enumerates per change.

Daily check-in. Promote only after a clean full soak window.

### Rollback

Cluster recreation is one-way. Rollback = revert the change on a hotfix branch, merge it, destroy + recreate again. Costs: one extra maintenance window per cluster, another round of Harbor/EFS loss, another IRSA refresh.

If the validation gates fail and the hotfix is small (parameter tweak, addon version), fix forward (`just deploy ${CLUSTER}` after merge) instead of destroying.

### References

- `docs/architecture.md`, `docs/operations.md`
- `scripts/destroy-cluster.sh` — destroy helper (step 5)
- `scripts/cluster-config.py` — resolves cluster-name / region / state-bucket / tfvars / module list from `clusters.yaml`
- `modules/eks/tests/smoke/` — base smoke assertions; add change-specific ones here
