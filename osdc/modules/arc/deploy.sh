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
export CNAME="$2"
export REGION="$3"
MODULE_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="${OSDC_ROOT:-$(cd "$MODULE_DIR/../.." && pwd)}"
UPSTREAM_ROOT="${OSDC_UPSTREAM:-$REPO_ROOT}"
# shellcheck source=/dev/null
source "$UPSTREAM_ROOT/scripts/mise-activate.sh"
CFG="$UPSTREAM_ROOT/scripts/cluster-config.py"

# Read per-installation ARC config (with defaults)
ARC_CHART_VERSION=$(uv run "$CFG" "$CLUSTER" arc.chart_version 0.14.0)
ARC_REPLICAS=$(uv run "$CFG" "$CLUSTER" arc.replica_count 2)
ARC_LOG_LEVEL=$(uv run "$CFG" "$CLUSTER" arc.log_level info)
ARC_CPU_REQ=$(uv run "$CFG" "$CLUSTER" arc.controller_cpu_request 1)
ARC_CPU_LIM=$(uv run "$CFG" "$CLUSTER" arc.controller_cpu_limit 4)
ARC_MEM_REQ=$(uv run "$CFG" "$CLUSTER" arc.controller_memory_request 2Gi)
ARC_MEM_LIM=$(uv run "$CFG" "$CLUSTER" arc.controller_memory_limit 4Gi)

echo "Installing ARC controller v${ARC_CHART_VERSION} (replicas=${ARC_REPLICAS}, logLevel=${ARC_LOG_LEVEL}, cpu=${ARC_CPU_REQ}/${ARC_CPU_LIM}, mem=${ARC_MEM_REQ}/${ARC_MEM_LIM})..."
helm upgrade --install arc \
  --namespace arc-systems \
  --create-namespace \
  --history-max 3 \
  -f "$MODULE_DIR/helm/arc/values.yaml" \
  --set replicaCount="${ARC_REPLICAS}" \
  --set log.level="${ARC_LOG_LEVEL}" \
  --set resources.requests.cpu="${ARC_CPU_REQ}" \
  --set resources.limits.cpu="${ARC_CPU_LIM}" \
  --set resources.requests.memory="${ARC_MEM_REQ}" \
  --set resources.limits.memory="${ARC_MEM_LIM}" \
  oci://ghcr.io/actions/actions-runner-controller-charts/gha-runner-scale-set-controller \
  --version "${ARC_CHART_VERSION}" \
  --timeout 10m \
  --wait

echo "ARC controller installed."
