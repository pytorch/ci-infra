#!/usr/bin/env bash
set -euo pipefail
#
# Bootstrap GCS state bucket for a GCP cluster.
#
# Creates:
#   - GCS bucket for state storage (versioned, uniform IAM)
#
# GCS provides native state locking — no DynamoDB equivalent needed.
#
# Usage:
#   ./scripts/bootstrap-state-gcp.sh <cluster-id>
#
# Idempotent: safe to run multiple times.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/mise-activate.sh"
CONFIG_PY="$SCRIPT_DIR/cluster-config.py"

CLUSTER="${1:?Usage: $0 <cluster-id>}"
PROJECT=$(uv run "$CONFIG_PY" "$CLUSTER" gcp_project)
BUCKET=$(uv run "$CONFIG_PY" "$CLUSTER" state_bucket)
REGION=$(uv run "$CONFIG_PY" "$CLUSTER" region)

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "Bootstrapping state for: $CLUSTER"
echo "  Bucket: $BUCKET (region: $REGION)"
echo "  Project: $PROJECT"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

if gcloud storage buckets describe "gs://${BUCKET}" --project="${PROJECT}" >/dev/null 2>&1; then
  echo "  Bucket '${BUCKET}' already exists, skipping create."
else
  echo "  Creating bucket '${BUCKET}'..."
  gcloud storage buckets create "gs://${BUCKET}" \
    --project="${PROJECT}" \
    --location="${REGION}" \
    --uniform-bucket-level-access
fi

echo "  Enabling versioning..."
gcloud storage buckets update "gs://${BUCKET}" --versioning

echo "  Done."
echo ""
echo "State bootstrapping complete."
