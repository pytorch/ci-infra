#!/usr/bin/env bash
set -euo pipefail
#
# Deploy NUMA-aware secondary scheduler (scheduler-plugins).
# Called by: just deploy-module <cluster> scheduler-plugins
#
# Args: $1=cluster-id  $2=cluster-name  $3=region
#
# Installs the scheduler-plugins Helm chart as a secondary scheduler
# named "numa-scheduler" with NodeResourceTopologyMatch enabled. Pods
# that set schedulerName: numa-scheduler get NUMA-aware placement.

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
ENABLED=$(uv run "$CFG" "$CLUSTER" scheduler_plugins.enabled "false")
if [[ "$ENABLED" != "true" ]]; then
  echo "Scheduler-plugins disabled for cluster $CLUSTER, skipping."
  exit 0
fi

SCHEDULER_PLUGINS_VERSION=$(uv run "$CFG" "$CLUSTER" scheduler_plugins.version "0.30.6")
SCHEDULER_REPLICAS=$(uv run "$CFG" "$CLUSTER" scheduler_plugins.replicas "2")

echo "Installing numa-scheduler (scheduler-plugins v${SCHEDULER_PLUGINS_VERSION})..."
helm_upgrade_if_changed numa-scheduler scheduler-plugins \
  --create-namespace \
  --history-max 3 \
  -f "$MODULE_DIR/helm/values.yaml" \
  --set scheduler.replicaCount="${SCHEDULER_REPLICAS}" \
  --timeout 5m \
  --wait \
  oci://ghcr.io/kubernetes-sigs/charts/scheduler-plugins \
  --version "${SCHEDULER_PLUGINS_VERSION}"

echo "numa-scheduler deployed."
