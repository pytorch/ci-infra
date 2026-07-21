#!/usr/bin/env bash
set -euo pipefail
#
# Deploy bin-pack-scheduler with a per-cluster replica count.
# Called by: just deploy-module <cluster> bin-pack-scheduler
# Args: $1=cluster-id  $2=cluster-name  $3=region

CLUSTER="$1"

MODULE_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="${OSDC_ROOT:-$(cd "$MODULE_DIR/../.." && pwd)}"
UPSTREAM_ROOT="${OSDC_UPSTREAM:-$REPO_ROOT}"
# shellcheck source=/dev/null
source "$UPSTREAM_ROOT/scripts/mise-activate.sh"
# shellcheck source=/dev/null
source "$UPSTREAM_ROOT/scripts/kubectl-apply.sh"
CFG="$UPSTREAM_ROOT/scripts/cluster-config.py"

REPLICAS=$(uv run "$CFG" "$CLUSTER" bin_pack_scheduler.replicas 3)
if [[ ! "$REPLICAS" =~ ^[1-9][0-9]*$ ]]; then
  echo "bin-pack-scheduler: bin_pack_scheduler.replicas must be a positive integer, got '$REPLICAS'" >&2
  exit 1
fi

rendered=$(kubectl kustomize "$MODULE_DIR/kubernetes/base" | sed "s/^  replicas: .*/  replicas: ${REPLICAS}/")
replica_lines=$(grep -c "^  replicas: ${REPLICAS}\$" <<<"$rendered" || true)
if [[ "$replica_lines" -ne 1 ]]; then
  echo "bin-pack-scheduler: expected exactly one 'replicas: ${REPLICAS}' line after substitution, got ${replica_lines}" >&2
  exit 1
fi
kubectl_apply_if_changed -f - <<<"$rendered"
