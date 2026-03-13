#!/usr/bin/env bash
set -euo pipefail
#
# Monitoring module deploy script.
# Called by: just deploy-module <cluster> monitoring
# Args: $1=cluster-id  $2=cluster-name  $3=region
#
# Deploys:
#   1. Monitoring namespace
#   2. kube-prometheus-stack Helm chart (Prometheus, Grafana, AlertManager,
#      node-exporter, kube-state-metrics) — provides monitoring.coreos.com CRDs
#   3. Custom ServiceMonitors/PodMonitors + DCGM exporter DaemonSet
#   4. Grafana Alloy (if grafana-cloud-credentials secret exists)

CLUSTER="$1"
export CNAME="$2"
export REGION="$3"
MODULE_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="${OSDC_ROOT:-$(cd "$MODULE_DIR/../.." && pwd)}"
UPSTREAM_ROOT="${OSDC_UPSTREAM:-$REPO_ROOT}"
# shellcheck source=/dev/null
source "$UPSTREAM_ROOT/scripts/mise-activate.sh"
CFG="$UPSTREAM_ROOT/scripts/cluster-config.py"

# --- Read per-installation monitoring config ---
NAMESPACE=$(uv run "$CFG" "$CLUSTER" monitoring.namespace monitoring)
RETENTION_DAYS=$(uv run "$CFG" "$CLUSTER" monitoring.retention_days 15)
STORAGE_SIZE=$(uv run "$CFG" "$CLUSTER" monitoring.storage_size 50Gi)
GRAFANA_ENABLED=$(uv run "$CFG" "$CLUSTER" monitoring.grafana_enabled true)
ALERTMANAGER_ENABLED=$(uv run "$CFG" "$CLUSTER" monitoring.alertmanager_enabled true)
# --- Create namespace ---
echo "Ensuring namespace '${NAMESPACE}' exists..."
kubectl create namespace "$NAMESPACE" 2>/dev/null || true

# --- Install kube-prometheus-stack ---
# Must be installed BEFORE applying ServiceMonitors/PodMonitors because the
# Helm chart provides the monitoring.coreos.com CRDs they depend on.
echo "Installing kube-prometheus-stack (retention=${RETENTION_DAYS}d, storage=${STORAGE_SIZE}, grafana=${GRAFANA_ENABLED}, alertmanager=${ALERTMANAGER_ENABLED})..."
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts 2>/dev/null || true
helm repo update prometheus-community

HELM_ARGS=(
    --version 82.10.3
    --namespace "$NAMESPACE"
    -f "$MODULE_DIR/helm/values.yaml"
    --set prometheus.prometheusSpec.retention="${RETENTION_DAYS}d"
    --set prometheus.prometheusSpec.storageSpec.volumeClaimTemplate.spec.resources.requests.storage="$STORAGE_SIZE"
    --set prometheus.prometheusSpec.externalLabels.cluster="$CNAME"
    --set grafana.enabled="$GRAFANA_ENABLED"
    --set alertmanager.enabled="$ALERTMANAGER_ENABLED"
    --timeout 10m
    --wait
)

helm upgrade --install kube-prometheus-stack \
    prometheus-community/kube-prometheus-stack \
    "${HELM_ARGS[@]}"

echo "kube-prometheus-stack installed."

# --- Apply ServiceMonitors, PodMonitors, and DCGM ServiceMonitor ---
# Applied AFTER kube-prometheus-stack because it provides the
# monitoring.coreos.com CRDs (ServiceMonitor, PodMonitor).
# The main kubernetes/kustomization.yaml (namespace + DCGM DaemonSet) is
# applied by the justfile before deploy.sh runs.
echo "Applying monitors (ServiceMonitors + PodMonitors)..."
kubectl apply -k "$MODULE_DIR/kubernetes/monitors/"

# --- Optionally install Alloy for Grafana Cloud push ---
# Gate: only install when the grafana-cloud-credentials secret exists.
# Alloy independently discovers ServiceMonitor/PodMonitor CRDs, scrapes targets,
# and pushes metrics to Grafana Cloud — fully decoupled from Prometheus.
if kubectl get secret grafana-cloud-credentials -n "$NAMESPACE" &>/dev/null; then
    GRAFANA_CLOUD_URL=$(uv run "$CFG" "$CLUSTER" monitoring.grafana_cloud_url "")
    if [[ -z "$GRAFANA_CLOUD_URL" ]]; then
        echo "WARNING: grafana-cloud-credentials secret found but no grafana_cloud_url in clusters.yaml. Skipping Alloy."
    else
        echo "Installing Alloy (pushing to ${GRAFANA_CLOUD_URL})..."
        helm repo add grafana https://grafana.github.io/helm-charts 2>/dev/null || true
        helm repo update grafana

        # Build a temporary override with per-cluster env vars
        ALLOY_OVERRIDE=$(mktemp)
        cat > "$ALLOY_OVERRIDE" <<EOF
alloy:
  extraEnv:
    - name: GCLOUD_RW_API_USER
      valueFrom:
        secretKeyRef:
          name: grafana-cloud-credentials
          key: username
    - name: GCLOUD_RW_API_KEY
      valueFrom:
        secretKeyRef:
          name: grafana-cloud-credentials
          key: password
    - name: GCLOUD_RW_URL
      value: "${GRAFANA_CLOUD_URL}"
    - name: CLUSTER_NAME
      value: "${CNAME}"
EOF

        helm upgrade --install alloy grafana/alloy \
            --namespace "$NAMESPACE" \
            -f "$MODULE_DIR/helm/alloy-values.yaml" \
            -f "$ALLOY_OVERRIDE" \
            --timeout 5m \
            --wait

        rm -f "$ALLOY_OVERRIDE"
        echo "Alloy installed — pushing metrics to Grafana Cloud."
    fi
else
    echo "No grafana-cloud-credentials secret found, skipping Alloy (metrics stay in-cluster)."
fi

echo "Monitoring module deployed."
