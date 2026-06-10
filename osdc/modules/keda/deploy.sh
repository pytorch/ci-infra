#!/usr/bin/env bash
set -euo pipefail
#
# KEDA module deploy script.
# Called by: just deploy-module <cluster> keda
# Args: $1=cluster-id  $2=cluster-name  $3=region
#
# Installs the KEDA operator (provides the ScaledObject/TriggerAuthentication
# CRDs that the buildkit module uses to autoscale builders).

CLUSTER="$1"
MODULE_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="${OSDC_ROOT:-$(cd "$MODULE_DIR/../.." && pwd)}"
UPSTREAM_ROOT="${OSDC_UPSTREAM:-$REPO_ROOT}"
# shellcheck source=/dev/null
source "$UPSTREAM_ROOT/scripts/mise-activate.sh"
# shellcheck source=/dev/null
source "$UPSTREAM_ROOT/scripts/helm-upgrade.sh"
CFG="$UPSTREAM_ROOT/scripts/cluster-config.py"

NAMESPACE="keda"
CHART_VERSION=$(uv run "$CFG" "$CLUSTER" keda.chart_version 2.16.1)

helm repo add kedacore https://kedacore.github.io/charts >/dev/null 2>&1 || true
helm repo update kedacore >/dev/null 2>&1 || true

helm_upgrade_if_changed keda "$NAMESPACE" \
  --create-namespace \
  --version "$CHART_VERSION" \
  -f "$MODULE_DIR/helm/values.yaml" \
  --timeout 10m \
  --wait \
  kedacore/keda

echo "KEDA deployed (chart $CHART_VERSION)."
