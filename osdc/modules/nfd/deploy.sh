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
#
# Also deploys a taint-remover DaemonSet that removes the
# node-init.osdc.io/nfd-topology startup taint from p5 nodes,
# unblocking workflow scheduling after NFD starts.

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

NFD_VERSION=$(uv run "$CFG" "$CLUSTER" nfd.version "0.17.1")
NFD_UPDATE_INTERVAL=$(uv run "$CFG" "$CLUSTER" nfd.update_interval "15s")

helm repo add nfd https://kubernetes-sigs.github.io/node-feature-discovery/charts 2>/dev/null || true
helm repo update nfd

echo "Installing NFD topology-updater v${NFD_VERSION}..."
helm_upgrade_if_changed nfd nfd \
  --create-namespace \
  --history-max 3 \
  -f "$MODULE_DIR/helm/values.yaml" \
  --set topologyUpdater.updateInterval="${NFD_UPDATE_INTERVAL}" \
  --timeout 5m \
  --wait \
  nfd/node-feature-discovery \
  --version "${NFD_VERSION}"

echo "NFD topology-updater deployed."

# --- Deploy taint-remover DaemonSet ---
# Two ConfigMaps:
#   nfd-taint-remover-script — wait-for-nrt.py (polls for NRT, then removes taint)
#   nfd-taint-remover-lib    — shared taint_remover.py library
# Then apply the DaemonSet that mounts both.
TAINT_REMOVER_LIB="$UPSTREAM_ROOT/base/kubernetes/node-taint-remover/lib/taint_remover.py"
WAIT_SCRIPT="$MODULE_DIR/scripts/wait-for-nrt.py"
if [[ ! -f "$TAINT_REMOVER_LIB" ]]; then
  echo "WARNING: taint_remover.py not found at $TAINT_REMOVER_LIB — skipping taint-remover deploy" >&2
elif [[ ! -f "$WAIT_SCRIPT" ]]; then
  echo "WARNING: wait-for-nrt.py not found at $WAIT_SCRIPT — skipping taint-remover deploy" >&2
else
  echo "Deploying NFD taint-remover..."
  kubectl create configmap nfd-taint-remover-script \
    --from-file="wait-for-nrt.py=$WAIT_SCRIPT" \
    -n nfd \
    --dry-run=client \
    -o yaml | kubectl apply -f -

  kubectl create configmap nfd-taint-remover-lib \
    --from-file="taint_remover.py=$TAINT_REMOVER_LIB" \
    -n nfd \
    --dry-run=client \
    -o yaml | kubectl apply -f -

  kubectl apply -f "$MODULE_DIR/kubernetes/nfd-taint-remover.yaml"
  echo "NFD taint-remover deployed."
fi
