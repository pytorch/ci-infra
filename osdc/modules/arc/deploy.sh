#!/usr/bin/env bash
set -euo pipefail
#
# ARC module deploy script.
# Called by: just deploy-module <cluster> arc
# Args: $1=cluster-id  $2=cluster-name  $3=region
#
# Installs the ARC controller Helm chart with per-installation config
# from clusters.yaml.

CLUSTER="$1"
CNAME="$2"
REGION="$3"
MODULE_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$MODULE_DIR/../.." && pwd)"
source "$REPO_ROOT/scripts/mise-activate.sh"
CFG="$REPO_ROOT/scripts/cluster-config.py"

# Read per-installation ARC config (with defaults)
ARC_REPLICAS=$(uv run "$CFG" "$CLUSTER" arc.replica_count 1)
ARC_LOG_LEVEL=$(uv run "$CFG" "$CLUSTER" arc.log_level info)

echo "Installing ARC controller (replicas=${ARC_REPLICAS}, logLevel=${ARC_LOG_LEVEL})..."
helm upgrade --install arc \
    --namespace arc-systems \
    --create-namespace \
    -f "$MODULE_DIR/helm/arc/values.yaml" \
    --set replicaCount="${ARC_REPLICAS}" \
    --set log.level="${ARC_LOG_LEVEL}" \
    oci://ghcr.io/actions/actions-runner-controller-charts/gha-runner-scale-set-controller \
    --timeout 10m \
    --wait

echo "ARC controller installed."
