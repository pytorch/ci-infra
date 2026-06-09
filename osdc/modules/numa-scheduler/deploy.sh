#!/usr/bin/env bash
set -euo pipefail
#
# Deploy NUMA-aware secondary scheduler (scheduler-plugins).
# Called by: just deploy-module <cluster> numa-scheduler
#
# Args: $1=cluster-id  $2=cluster-name  $3=region
#
# Downloads the scheduler-plugins Helm chart from the GitHub release
# and installs it as a secondary scheduler named "numa-scheduler" with
# NodeResourceTopologyMatch enabled. Pods that set
# schedulerName: numa-scheduler get NUMA-aware placement.

CLUSTER="$1"
_CNAME="$2"  # unused but required by deploy-module interface
_REGION="$3" # unused but required by deploy-module interface
MODULE_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="${OSDC_ROOT:-$(cd "$MODULE_DIR/../.." && pwd)}"
UPSTREAM_ROOT="${OSDC_UPSTREAM:-$REPO_ROOT}"

# shellcheck source=/dev/null
source "$UPSTREAM_ROOT/scripts/mise-activate.sh"
# shellcheck source=/dev/null
source "$UPSTREAM_ROOT/scripts/helm-upgrade.sh"

CFG="$UPSTREAM_ROOT/scripts/cluster-config.py"

# --- Check if enabled ---
ENABLED=$(uv run "$CFG" "$CLUSTER" numa_scheduler.enabled "false")
if [[ "$ENABLED" != "true" ]]; then
  echo "numa-scheduler disabled for cluster $CLUSTER, skipping."
  exit 0
fi

CHART_VERSION=$(uv run "$CFG" "$CLUSTER" numa_scheduler.chart_version "0.34.7")
SCHEDULER_REPLICAS=$(uv run "$CFG" "$CLUSTER" numa_scheduler.replicas "2")

# --- Download chart from GitHub release ---
CHART_TGZ="scheduler-plugins-${CHART_VERSION}.tgz"
CHART_DIR=$(mktemp -d)
trap 'rm -rf "$CHART_DIR"' EXIT

echo "Downloading scheduler-plugins chart v${CHART_VERSION}..."
gh release download "v${CHART_VERSION}" \
  --repo kubernetes-sigs/scheduler-plugins \
  --pattern "${CHART_TGZ}" \
  --dir "$CHART_DIR"

echo "Installing numa-scheduler (scheduler-plugins v${CHART_VERSION})..."
helm_upgrade_if_changed numa-scheduler numa-scheduler \
  --create-namespace \
  --history-max 3 \
  -f "$MODULE_DIR/helm/values.yaml" \
  --set scheduler.replicaCount="${SCHEDULER_REPLICAS}" \
  --timeout 5m \
  --wait \
  "${CHART_DIR}/${CHART_TGZ}"

echo "numa-scheduler deployed."
