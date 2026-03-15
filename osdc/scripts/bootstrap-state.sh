#!/usr/bin/env bash
set -euo pipefail
#
# Bootstrap Terraform/OpenTofu remote state for a cluster.
#
# Creates:
#   - S3 bucket for state storage (versioned, encrypted, private)
#   - DynamoDB table for state locking (shared across all clusters)
#
# Usage:
#   ./scripts/bootstrap-state.sh <cluster-id>
#   ./scripts/bootstrap-state.sh arc-staging
#   ./scripts/bootstrap-state.sh --all              # bootstrap all clusters
#
# Idempotent: safe to run multiple times.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/mise-activate.sh"
CONFIG_PY="$SCRIPT_DIR/cluster-config.py"

LOCK_TABLE="ciforge-terraform-locks"
# State buckets live in a fixed region regardless of cluster region
STATE_REGION="us-west-2"

bootstrap_cluster() {
  local cluster_id="$1"
  local bucket
  bucket=$(uv run "$CONFIG_PY" "$cluster_id" state_bucket)

  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo "Bootstrapping state for: $cluster_id"
  echo "  Bucket: $bucket (region: $STATE_REGION)"
  echo "  Lock table: $LOCK_TABLE (region: $STATE_REGION)"
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

  # Create S3 bucket
  if aws s3api head-bucket --bucket "$bucket" --region "$STATE_REGION" 2>/dev/null; then
    echo "  Bucket '$bucket' already exists, skipping."
  else
    echo "  Creating bucket '$bucket'..."
    # us-east-1 doesn't accept LocationConstraint
    if [[ "$STATE_REGION" == "us-east-1" ]]; then
      aws s3api create-bucket \
        --bucket "$bucket" \
        --region "$STATE_REGION"
    else
      aws s3api create-bucket \
        --bucket "$bucket" \
        --region "$STATE_REGION" \
        --create-bucket-configuration LocationConstraint="$STATE_REGION"
    fi
  fi

  # Enable versioning
  echo "  Enabling versioning..."
  aws s3api put-bucket-versioning \
    --bucket "$bucket" \
    --region "$STATE_REGION" \
    --versioning-configuration Status=Enabled

  # Enable encryption
  echo "  Enabling encryption..."
  aws s3api put-bucket-encryption \
    --bucket "$bucket" \
    --region "$STATE_REGION" \
    --server-side-encryption-configuration '{
            "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "aws:kms"},"BucketKeyEnabled": true}]
        }'

  # Block public access
  echo "  Blocking public access..."
  aws s3api put-public-access-block \
    --bucket "$bucket" \
    --region "$STATE_REGION" \
    --public-access-block-configuration \
    BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true

  echo "  Done."
  echo ""
}

bootstrap_lock_table() {
  echo "Ensuring DynamoDB lock table '$LOCK_TABLE' exists in $STATE_REGION..."
  if aws dynamodb describe-table --table-name "$LOCK_TABLE" --region "$STATE_REGION" &>/dev/null; then
    echo "  Lock table already exists, skipping."
  else
    echo "  Creating lock table..."
    aws dynamodb create-table \
      --table-name "$LOCK_TABLE" \
      --attribute-definitions AttributeName=LockID,AttributeType=S \
      --key-schema AttributeName=LockID,KeyType=HASH \
      --billing-mode PAY_PER_REQUEST \
      --region "$STATE_REGION"
    aws dynamodb wait table-exists --table-name "$LOCK_TABLE" --region "$STATE_REGION"
  fi
  echo ""
}

# --- Main ---

if [[ "${1:-}" == "--all" ]]; then
  clusters=$(uv run "$CONFIG_PY" --list)
elif [[ -n "${1:-}" ]]; then
  clusters="$1"
else
  echo "Usage: $0 <cluster-id|--all>"
  echo ""
  echo "Available clusters:"
  uv run "$CONFIG_PY" --list | sed 's/^/  /'
  exit 1
fi

bootstrap_lock_table

for cluster_id in $clusters; do
  bootstrap_cluster "$cluster_id"
done

echo "State bootstrapping complete."
