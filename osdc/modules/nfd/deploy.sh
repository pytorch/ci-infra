#!/usr/bin/env bash
set -euo pipefail
#
# Deploy NFD topology-updater for NUMA-aware scheduling.
# Called by: just deploy-module <cluster> nfd
#
# Args: $1=cluster-id  $2=cluster-name  $3=region
#
# Installs the Node Feature Discovery Helm chart with only the
# topology-updater enabled. This publishes NodeResourceTopology CRDs
# that the numa-scheduler reads to make NUMA-aware placement decisions.

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
ENABLED=$(uv run "$CFG" "$CLUSTER" nfd.enabled "false")
if [[ "$ENABLED" != "true" ]]; then
  echo "NFD disabled for cluster $CLUSTER, skipping."
  exit 0
fi

NFD_VERSION=$(uv run "$CFG" "$CLUSTER" nfd.version "0.17.1")
NFD_UPDATE_INTERVAL=$(uv run "$CFG" "$CLUSTER" nfd.update_interval "15s")

echo "Installing NFD topology-updater v${NFD_VERSION}..."
helm_upgrade_if_changed nfd nfd \
  --create-namespace \
  --history-max 3 \
  -f "$MODULE_DIR/helm/values.yaml" \
  --set topologyUpdater.updateInterval="${NFD_UPDATE_INTERVAL}" \
  --timeout 5m \
  --wait \
  oci://ghcr.io/kubernetes-sigs/charts/node-feature-discovery \
  --version "${NFD_VERSION}"

echo "NFD topology-updater deployed."
