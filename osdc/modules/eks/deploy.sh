#!/usr/bin/env bash
set -euo pipefail
#
# EKS base module deploy script.
# Called by: just deploy-module (as the first module in the list)
# Args: $1=cluster-id  $2=cluster-name  $3=region
#
# Handles core AWS/EKS infrastructure:
#   1. Terraform: VPC + EKS cluster + Harbor S3/IAM
#   2. Kubeconfig
#   3. Mirror Harbor bootstrap images to ECR
#
# Other components (harbor, git-cache, node-compactor, etc.) are
# deployed as separate modules via deploy-module.

CLUSTER="$1"
CNAME="$2"
REGION="$3"
MODULE_DIR="$(cd "$(dirname "$0")" && pwd)"
UPSTREAM_ROOT="${OSDC_UPSTREAM:-$(cd "$MODULE_DIR/../.." && pwd)}"
# shellcheck source=/dev/null
source "$UPSTREAM_ROOT/scripts/mise-activate.sh"
CFG="$UPSTREAM_ROOT/scripts/cluster-config.py"

BUCKET=$(uv run "$CFG" "$CLUSTER" state_bucket)
TFVARS=$(uv run "$CFG" "$CLUSTER" tfvars)
STATE_REGION="us-west-2" # state buckets always in us-west-2

# ─── Preflight ────────────────────────────────────────────────────────────
if ! aws s3api head-bucket --bucket "${BUCKET}" --region "${STATE_REGION}" 2>/dev/null; then
  echo ""
  echo "ERROR: State bucket '${BUCKET}' does not exist."
  echo ""
  echo "Run bootstrap first:"
  echo "  just bootstrap ${CLUSTER}"
  echo ""
  exit 1
fi

# ─── Phase 1: Terraform ──────────────────────────────────────────────────
echo ""
echo "━━━ EKS: Terraform ━━━"
cd "$MODULE_DIR/infra"
tofu init -reconfigure \
  -backend-config="bucket=${BUCKET}" \
  -backend-config="key=${CLUSTER}/base/terraform.tfstate" \
  -backend-config="region=${STATE_REGION}" \
  -backend-config="dynamodb_table=ciforge-terraform-locks"

set +e
# shellcheck disable=SC2086
eval tofu plan $TFVARS -out=tfplan -detailed-exitcode
PLAN_EXIT=$?
set -e

if [[ $PLAN_EXIT -eq 0 ]]; then
  echo "No changes. Skipping apply."
  rm -f tfplan
elif [[ $PLAN_EXIT -eq 2 ]]; then
  echo ""
  CONFIRM="${OSDC_CONFIRM:-ask}"
  if [[ "$CONFIRM" == "yes" ]]; then
    echo "Auto-confirmed (OSDC_CONFIRM=yes)"
  elif [[ "$CONFIRM" == "no" ]]; then
    rm -f tfplan
    echo "Cancelled (OSDC_CONFIRM=no)."
    exit 1
  else
    read -p "Apply this plan? [y/N] " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
      rm -f tfplan
      echo "Cancelled."
      exit 1
    fi
  fi
  tofu apply tfplan
  rm -f tfplan
else
  rm -f tfplan
  echo "Tofu plan failed."
  exit 1
fi
cd - >/dev/null

# ─── Phase 2: Kubeconfig ─────────────────────────────────────────────────
echo ""
echo "━━━ EKS: Kubeconfig ━━━"
echo "Updating kubeconfig for $CNAME ($REGION)..."
NO_PROXY="${NO_PROXY:-},.eks.amazonaws.com" no_proxy="${no_proxy:-},.eks.amazonaws.com" \
  "$UPSTREAM_ROOT/scripts/kubeconfig-lock.sh" --name "$CNAME" --region "$REGION" --alias "$CNAME"

# ─── Phase 3: Mirror bootstrap images ────────────────────────────────────
echo ""
echo "━━━ EKS: Mirror bootstrap images ━━━"
"$MODULE_DIR/scripts/mirror-images.sh" "$CLUSTER"

echo ""
echo "EKS base module deployed."
