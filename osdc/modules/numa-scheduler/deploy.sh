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
export CNAME="$2"
export REGION="$3"
MODULE_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="${OSDC_ROOT:-$(cd "$MODULE_DIR/../.." && pwd)}"
UPSTREAM_ROOT="${OSDC_UPSTREAM:-$REPO_ROOT}"
# shellcheck source=/dev/null
source "$UPSTREAM_ROOT/scripts/mise-activate.sh"
# shellcheck source=/dev/null
source "$UPSTREAM_ROOT/scripts/helm-upgrade.sh"
CFG="$UPSTREAM_ROOT/scripts/cluster-config.py"

CHART_VERSION=$(uv run "$CFG" "$CLUSTER" numa_scheduler.chart_version "0.34.7")
SCHEDULER_REPLICAS=$(uv run "$CFG" "$CLUSTER" numa_scheduler.replicas "2")

# --- Download chart from GitHub release ---
CHART_TGZ="scheduler-plugins-${CHART_VERSION}.tgz"
CHART_DIR=$(mktemp -d)
trap 'rm -rf "$CHART_DIR"' EXIT

CHART_URL="https://github.com/kubernetes-sigs/scheduler-plugins/releases/download/v${CHART_VERSION}/${CHART_TGZ}"
echo "Downloading scheduler-plugins chart v${CHART_VERSION}..."
curl -fsSL "$CHART_URL" -o "${CHART_DIR}/${CHART_TGZ}"

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
