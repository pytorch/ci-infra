#!/usr/bin/env bash
set -euo pipefail
#
# Logging base component deploy script.
# Called from the justfile's deploy-base recipe.
#
# Args: $1=cluster-id
#
# Deploys:
#   1. Logging namespace
#   2. Assembled Alloy config ConfigMap (base pipeline + per-module pipelines)
#   3. Grafana Alloy DaemonSet (if grafana-cloud-credentials secret exists)
#   4. Grafana Alloy Deployment for Kubernetes Event collection

CLUSTER="$1"
MODULE_DIR="$(cd "$(dirname "$0")" && pwd)"
OSDC_UPSTREAM="${OSDC_UPSTREAM:-$(cd "$MODULE_DIR/../.." && pwd)}"
# shellcheck source=/dev/null
source "$OSDC_UPSTREAM/scripts/mise-activate.sh"
CFG="$OSDC_UPSTREAM/scripts/cluster-config.py"
CLUSTERS_YAML="${CLUSTERS_YAML:-$OSDC_UPSTREAM/clusters.yaml}"

# --- Read per-installation logging config ---
NAMESPACE=$(uv run "$CFG" "$CLUSTER" logging.namespace logging)
CNAME=$(uv run "$CFG" "$CLUSTER" cluster_name)
LOKI_URL=$(uv run "$CFG" "$CLUSTER" logging.grafana_cloud_loki_url "")

if [[ -z "$LOKI_URL" ]]; then
  echo "Error: logging.grafana_cloud_loki_url is not configured for cluster '$CLUSTER'"
  exit 1
fi

# --- Create namespace ---
echo "Ensuring namespace '${NAMESPACE}' exists..."
kubectl create namespace "$NAMESPACE" 2>/dev/null || true

# --- Gate on grafana-cloud-credentials secret ---
if ! kubectl get secret grafana-cloud-credentials -n "$NAMESPACE" &>/dev/null; then
  echo "No grafana-cloud-credentials secret found in '${NAMESPACE}', skipping logging deploy."
  exit 0
fi

# --- Cleanup trap ---
CONFIGMAP_FILE=""
ALLOY_OVERRIDE=""
EVENTS_OVERRIDE=""
cleanup() {
  [[ -n "$CONFIGMAP_FILE" ]] && rm -f "$CONFIGMAP_FILE" 2>/dev/null || true
  [[ -n "$ALLOY_OVERRIDE" ]] && rm -f "$ALLOY_OVERRIDE" 2>/dev/null || true
  [[ -n "$EVENTS_OVERRIDE" ]] && rm -f "$EVENTS_OVERRIDE" 2>/dev/null || true
}
trap cleanup EXIT

# --- Assemble Alloy config ConfigMap ---
OSDC_ROOT="${OSDC_ROOT:-$OSDC_UPSTREAM}"
CONFIGMAP_FILE=$(mktemp)

echo "Assembling Alloy logging config..."
uv run "$MODULE_DIR/scripts/python/assemble_config.py" \
  --base-pipeline "$MODULE_DIR/pipelines/base.alloy" \
  --modules-dir "$OSDC_ROOT/modules" \
  --upstream-modules-dir "$OSDC_UPSTREAM/modules" \
  --cluster "$CLUSTER" \
  --clusters-yaml "$CLUSTERS_YAML" \
  --namespace "$NAMESPACE" \
  --output "$CONFIGMAP_FILE"

kubectl apply -f "$CONFIGMAP_FILE"

# --- Build Helm override with per-cluster env vars ---
ALLOY_OVERRIDE=$(mktemp)
cat >"$ALLOY_OVERRIDE" <<EOF
alloy:
  extraEnv:
    - name: CLUSTER_NAME
      value: "${CNAME}"
    - name: LOKI_URL
      value: "${LOKI_URL}"
    - name: LOKI_USERNAME
      valueFrom:
        secretKeyRef:
          name: grafana-cloud-credentials
          key: loki-username
    - name: LOKI_API_KEY
      valueFrom:
        secretKeyRef:
          name: grafana-cloud-credentials
          key: loki-api-key-write
    - name: GOMEMLIMIT
      value: "1800MiB"
    - name: NODE_NAME
      valueFrom:
        fieldRef:
          fieldPath: spec.nodeName
  configMap:
    create: false
    name: alloy-logging-config
    key: config.alloy
EOF

# --- Install Alloy via Helm ---
echo "Installing Alloy logging DaemonSet (pushing to ${LOKI_URL})..."
helm repo add grafana https://grafana.github.io/helm-charts 2>/dev/null || true
helm repo update grafana

ALLOY_CHART_VERSION=$(uv run "$CFG" "$CLUSTER" monitoring.alloy_chart_version 1.6.2)
helm upgrade --install alloy-logging grafana/alloy \
  --namespace "$NAMESPACE" \
  --history-max 3 \
  -f "$MODULE_DIR/helm/alloy-logging-values.yaml" \
  -f "$ALLOY_OVERRIDE" \
  --version "${ALLOY_CHART_VERSION}" \
  --timeout 5m \
  --wait

# --- Install Alloy Events Deployment ---
echo "Installing Alloy events Deployment (K8s event collection)..."
EVENTS_OVERRIDE=$(mktemp)
cat >"$EVENTS_OVERRIDE" <<EOF
alloy:
  extraEnv:
    - name: CLUSTER_NAME
      value: "${CNAME}"
    - name: LOKI_URL
      value: "${LOKI_URL}"
    - name: LOKI_USERNAME
      valueFrom:
        secretKeyRef:
          name: grafana-cloud-credentials
          key: loki-username
    - name: LOKI_API_KEY
      valueFrom:
        secretKeyRef:
          name: grafana-cloud-credentials
          key: loki-api-key-write
    - name: GOMEMLIMIT
      value: "1800MiB"
EOF

helm upgrade --install alloy-events grafana/alloy \
  --namespace "$NAMESPACE" \
  --history-max 3 \
  -f "$MODULE_DIR/helm/alloy-events-values.yaml" \
  -f "$EVENTS_OVERRIDE" \
  --version "${ALLOY_CHART_VERSION}" \
  --timeout 5m \
  --wait

echo "Logging deployed — Alloy DaemonSet + Events Deployment pushing to Grafana Cloud Loki."
