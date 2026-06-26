#!/usr/bin/env bash
set -euo pipefail
#
# Destroy every tofu-managed module and the base for a cluster, in
# reverse-deploy order. Automation for step 6 of
# `docs/cluster-recreation.md`.
#
# Usage:
#   ./scripts/destroy-cluster.sh <cluster-id>
#
# Env vars:
#   OSDC_CONFIRM=yes   Skip the interactive confirmation prompts. The
#                      final base-destroy still requires the prompt unless
#                      you also set OSDC_CONFIRM_BASE=destroy.
#
# Prereqs (NOT enforced — operator must run them first):
#   1. just drain-runners <cluster>      (runner pods drained; NodeClaims gone)
#   2. Harbor S3 bucket emptied          (step 5 of the runbook)
#
# After the base is destroyed, sweeps for any EC2 still tagged
# eks:eks-cluster-name=<cluster_name> and terminates the orphans — backstop for
# a cluster destroyed before its Karpenter nodes drained (those otherwise keep
# running and hold their capacity reservation with nothing left to reap them).
#
# What this destroys:
#   - tofu-managed modules: karpenter, pypi-cache  (state keys
#     <cluster>/<module>/terraform.tfstate)
#   - base / eks: VPC, EKS control plane, Harbor S3, IAM, KMS
#     (state key <cluster>/base/terraform.tfstate)
#
# What this does NOT destroy:
#   - k8s-only modules (arc, arc-runners*, buildkit, cache-enforcer,
#     harbor-cache-recovery, logging, monitoring, nodepools*,
#     zombie-cleanup) — these have no terraform state; the resources
#     die with the EKS cluster.
#   - Tofu state bucket and DynamoDB lock table (shared, intentionally
#     preserved across recreations).

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/mise-activate.sh"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/state-config.sh"
: "${STATE_REGION:?state-config.sh did not export STATE_REGION}"

CONFIG_PY="$SCRIPT_DIR/cluster-config.py"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
LOCK_TABLE="ciforge-terraform-locks"

# ── Argument parsing ────────────────────────────────────────────────────────

if [[ $# -ne 1 ]]; then
  echo "Usage: $0 <cluster-id>" >&2
  echo "" >&2
  echo "Available clusters:" >&2
  uv run "$CONFIG_PY" --list | sed 's/^/  /' >&2
  exit 2
fi
CLUSTER="$1"

CNAME=$(uv run "$CONFIG_PY" "$CLUSTER" cluster_name)
REGION=$(uv run "$CONFIG_PY" "$CLUSTER" region)
BUCKET=$(uv run "$CONFIG_PY" "$CLUSTER" state_bucket)
TFVARS=$(uv run "$CONFIG_PY" "$CLUSTER" tfvars)

# ── Compute destroy order ──────────────────────────────────────────────────
# Reverse of the cluster's `modules` list, filtered to entries that actually
# have a terraform/ root. Anything else is k8s-only and dies with the cluster.

MODULES=()
while IFS= read -r m; do MODULES+=("$m"); done < <(uv run "$CONFIG_PY" "$CLUSTER" modules)

TF_MODULES=()
K8S_ONLY_MODULES=()
for ((i = ${#MODULES[@]} - 1; i >= 0; i--)); do
  m="${MODULES[i]}"
  if [[ -f "$ROOT/modules/$m/terraform/main.tf" ]]; then
    TF_MODULES+=("$m")
  else
    K8S_ONLY_MODULES+=("$m")
  fi
done

# ── Plan summary ───────────────────────────────────────────────────────────

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "DESTROY plan for cluster: $CLUSTER ($CNAME) in $REGION"
echo "  State bucket: $BUCKET (region: $STATE_REGION)"
echo "  Lock table:   $LOCK_TABLE"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "tofu destroy (in order):"
for m in "${TF_MODULES[@]}"; do
  echo "  - modules/$m         (state key: $CLUSTER/$m/terraform.tfstate)"
done
echo "  - modules/eks (base)  (state key: $CLUSTER/base/terraform.tfstate)"
echo ""
if [[ ${#K8S_ONLY_MODULES[@]} -gt 0 ]]; then
  echo "k8s-only modules (no terraform — die with the EKS cluster):"
  for m in "${K8S_ONLY_MODULES[@]}"; do echo "  - $m"; done
  echo ""
fi

# ── Preflight: NodeClaim drain warning ─────────────────────────────────────
# Best-effort only — if kubectl can't reach the cluster we silently skip.

if command -v kubectl >/dev/null 2>&1; then
  NC_COUNT=$(
    NO_PROXY="${NO_PROXY:-},.eks.amazonaws.com" \
      no_proxy="${no_proxy:-},.eks.amazonaws.com" \
      kubectl get nodeclaims --no-headers 2>/dev/null | wc -l | tr -d ' '
  )
  if [[ "$NC_COUNT" != "0" ]]; then
    echo "WARNING: $NC_COUNT Karpenter NodeClaim(s) still exist on this cluster."
    echo "         Run 'just drain-runners $CLUSTER' first — continuing may leak EC2."
    echo ""
  fi
fi

# ── Confirmation gate (overall) ────────────────────────────────────────────

CONFIRM="${OSDC_CONFIRM:-ask}"
if [[ "$CONFIRM" != "yes" ]]; then
  read -rp "Proceed with destroy? (type the cluster name to confirm): " CONFIRM_INPUT
  if [[ "$CONFIRM_INPUT" != "$CLUSTER" ]]; then
    echo "Mismatch — aborting."
    exit 1
  fi
fi
echo ""

# ── Helpers ────────────────────────────────────────────────────────────────

tofu_init() {
  local module_dir="$1"
  local key="$2"
  cd "$module_dir"
  NO_PROXY="${NO_PROXY:-},.amazonaws.com" \
    no_proxy="${no_proxy:-},.amazonaws.com" \
    tofu init -reconfigure -input=false -no-color \
    -backend-config="bucket=$BUCKET" \
    -backend-config="key=$key" \
    -backend-config="region=$STATE_REGION" \
    -backend-config="dynamodb_table=$LOCK_TABLE" >/dev/null
}

destroy_module() {
  local module="$1"
  local module_dir="$ROOT/modules/$module/terraform"
  local key="$CLUSTER/$module/terraform.tfstate"

  echo "━━━ DESTROY: $module ━━━"
  tofu_init "$module_dir" "$key"
  NO_PROXY="${NO_PROXY:-},.amazonaws.com" \
    no_proxy="${no_proxy:-},.amazonaws.com" \
    tofu destroy -lock-timeout=15m -auto-approve \
    -var="cluster_name=$CNAME" \
    -var="aws_region=$REGION" \
    -var="state_bucket=$BUCKET" \
    -var="cluster_id=$CLUSTER"
  cd - >/dev/null
  echo ""
}

destroy_base() {
  local module_dir="$ROOT/modules/eks/terraform"
  local key="$CLUSTER/base/terraform.tfstate"

  echo "━━━ DESTROY: base (modules/eks) ━━━"
  tofu_init "$module_dir" "$key"
  NO_PROXY="${NO_PROXY:-},.amazonaws.com" \
    no_proxy="${no_proxy:-},.amazonaws.com" \
    eval tofu destroy -lock-timeout=15m "$TFVARS" -auto-approve
  cd - >/dev/null
  echo ""
}

# Backstop: terminate any EC2 still tagged for this cluster once the control
# plane is gone — stuck Karpenter finalizers, or a destroy that ran before nodes
# drained. Such instances keep running (and hold their capacity reservation)
# with nothing left to reap them. Scoped to this cluster's cluster_name tag.
sweep_orphaned_instances() {
  echo "━━━ SWEEP: orphaned EC2 (tag eks:eks-cluster-name=$CNAME) ━━━"
  local np="${NO_PROXY:-},.amazonaws.com"
  local ids
  ids=$(NO_PROXY="$np" no_proxy="$np" aws ec2 describe-instances --region "$REGION" \
    --filters "Name=tag:eks:eks-cluster-name,Values=$CNAME" \
    "Name=instance-state-name,Values=running,pending,stopping,stopped" \
    --query 'Reservations[].Instances[].InstanceId' --output text 2>/dev/null || true)
  if [[ -z "${ids// /}" ]]; then
    echo "  None found. ✓"
    echo ""
    return 0
  fi
  echo "  Terminating orphaned instances: $ids"
  # shellcheck disable=SC2086  # space-separated instance IDs must word-split
  NO_PROXY="$np" no_proxy="$np" aws ec2 terminate-instances --region "$REGION" \
    --instance-ids $ids >/dev/null
  echo "  Termination requested — capacity reservations free as they shut down."
  echo ""
}

# ── Phase 1: destroy tofu-managed modules ──────────────────────────────────

for m in "${TF_MODULES[@]}"; do
  destroy_module "$m"
done

# ── Phase 2: destroy base (extra confirmation) ─────────────────────────────

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "FINAL STEP: tofu destroy modules/eks (the base)"
echo ""
echo "This destroys: VPC, EKS control plane, base node group, Harbor S3,"
echo "  IAM roles, KMS keys, OIDC provider."
echo ""
echo "The Harbor S3 bucket ($CNAME-harbor-registry) MUST be empty."
echo "  Step 5 of docs/cluster-recreation.md handles this."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

CONFIRM_BASE="${OSDC_CONFIRM_BASE:-ask}"
if [[ "$CONFIRM_BASE" != "destroy" ]]; then
  read -rp "Type 'destroy $CLUSTER' to proceed: " CONFIRM_INPUT
  if [[ "$CONFIRM_INPUT" != "destroy $CLUSTER" ]]; then
    echo "Mismatch — aborting before base destroy. Modules already destroyed; re-run to resume."
    exit 1
  fi
fi
echo ""

destroy_base

# ── Phase 3: reap any orphaned EC2 (e.g. destroy ran before nodes drained) ──

sweep_orphaned_instances

# ── Done ───────────────────────────────────────────────────────────────────

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "Done. Cluster $CLUSTER ($CNAME) destroyed."
echo ""
echo "Next steps per runbook:"
echo "  7. Verify survivors (ECR mirrors, state bucket, wheel-pipeline)"
echo "  8. just deploy $CLUSTER  (recreates with current main shape)"
echo "  9. just smoke $CLUSTER"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
