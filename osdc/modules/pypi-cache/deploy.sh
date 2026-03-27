#!/usr/bin/env bash
set -euo pipefail
#
# PyPI cache module deploy script.
# Called by: just deploy-module <cluster> pypi-cache
# Args: $1=cluster-id  $2=cluster-name  $3=region
#
# Deploys:
#   1. pypi-cache namespace + ServiceAccount (kustomize)
#   2. EFS-backed StorageClass + PVC
#   3. ConfigMaps (scripts + config)
#   4. Karpenter NodePools (if instance_type configured)
#   5. Per-CUDA-slug Deployments + Services
#
# Architecture: Deployment + EFS (shared filesystem), one Deployment per CUDA
# version plus CPU.  Replaces the old DaemonSet + NVMe hostPath approach.

CLUSTER="$1"
export CNAME="$2"
export REGION="$3"
MODULE_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="${OSDC_ROOT:-$(cd "$MODULE_DIR/../.." && pwd)}"
UPSTREAM_ROOT="${OSDC_UPSTREAM:-$REPO_ROOT}"
# shellcheck source=/dev/null
source "$UPSTREAM_ROOT/scripts/mise-activate.sh"
# shellcheck source=/dev/null
source "$UPSTREAM_ROOT/scripts/kubectl-apply.sh"
CFG="$UPSTREAM_ROOT/scripts/cluster-config.py"
CLUSTERS_YAML="${CLUSTERS_YAML:-$UPSTREAM_ROOT/clusters.yaml}"

# --- Read pypi_cache config ---
NAMESPACE=$(uv run "$CFG" "$CLUSTER" pypi_cache.namespace pypi-cache)
INSTANCE_TYPE=$(uv run "$CFG" "$CLUSTER" pypi_cache.instance_type "")
BUCKET=$(uv run "$CFG" "$CLUSTER" state_bucket)
STATE_REGION="us-west-2"

# --- Read terraform output: EFS filesystem ID ---
echo "[pypi-cache] Reading terraform outputs..."
cd "$MODULE_DIR/terraform"
tofu init -reconfigure \
  -backend-config="bucket=${BUCKET}" \
  -backend-config="key=${CLUSTER}/pypi-cache/terraform.tfstate" \
  -backend-config="region=${STATE_REGION}" \
  -backend-config="dynamodb_table=ciforge-terraform-locks" \
  >/dev/null 2>&1
EFS_FS_ID=$(tofu output -raw efs_filesystem_id)
cd - >/dev/null
echo "[pypi-cache] EFS filesystem ID: ${EFS_FS_ID}"

# --- Apply base k8s resources (namespace, SA) ---
echo "[pypi-cache] Applying base resources (namespace, ServiceAccount)..."
kubectl_apply_if_changed -k "$MODULE_DIR/kubernetes/"

# --- Create ConfigMaps (must exist before Deployments reference them) ---
echo "[pypi-cache] Creating pypi-cache-scripts ConfigMap..."
kubectl create configmap pypi-cache-scripts \
  --from-file="$MODULE_DIR/scripts/python/log_rotator.py" \
  -n "$NAMESPACE" \
  --dry-run=client -o yaml | kubectl apply -f -

# Look up kube-dns ClusterIP for nginx resolver directive.
# Different EKS clusters use different service CIDRs, so this
# must be discovered at deploy time rather than hardcoded.
DNS_RESOLVER=$(kubectl get svc kube-dns -n kube-system -o jsonpath='{.spec.clusterIP}')
echo "[pypi-cache] kube-dns ClusterIP: ${DNS_RESOLVER}"

echo "[pypi-cache] Creating pypi-cache-nginx-config ConfigMap..."
sed "s/__DNS_RESOLVER__/${DNS_RESOLVER}/g" "$MODULE_DIR/kubernetes/nginx.conf" \
  | kubectl create configmap pypi-cache-nginx-config \
    --from-file=nginx.conf=/dev/stdin \
    -n "$NAMESPACE" \
    --dry-run=client -o yaml | kubectl apply -f -

# --- Generate manifests from clusters.yaml config ---
GENERATED_DIR="$MODULE_DIR/generated"
mkdir -p "$GENERATED_DIR"

echo "[pypi-cache] Generating manifests..."
GENERATE_ARGS=(
  "--cluster" "$CLUSTER"
  "--clusters-yaml" "$CLUSTERS_YAML"
  "--efs-filesystem-id" "$EFS_FS_ID"
  "--output-dir" "$GENERATED_DIR"
)
if [[ -n "$INSTANCE_TYPE" ]]; then
  GENERATE_ARGS+=("--instance-type" "$INSTANCE_TYPE")
fi
uv run "$MODULE_DIR/scripts/python/generate_manifests.py" "${GENERATE_ARGS[@]}"

# --- Apply generated manifests in dependency order ---
echo "[pypi-cache] Applying StorageClass..."
kubectl_apply_if_changed -f "$GENERATED_DIR/storageclass.yaml"

echo "[pypi-cache] Applying PVC..."
kubectl_apply_if_changed -f "$GENERATED_DIR/pvc.yaml"

# Apply NodePools (if generated — only when instance_type is configured)
if [[ -f "$GENERATED_DIR/nodepools.yaml" ]]; then
  echo "[pypi-cache] Applying NodePools..."
  sed "s/CLUSTER_NAME_PLACEHOLDER/$CNAME/g" "$GENERATED_DIR/nodepools.yaml" \
    | kubectl_apply_if_changed -f -
fi

echo "[pypi-cache] Applying Services..."
kubectl_apply_if_changed -f "$GENERATED_DIR/services.yaml"

echo "[pypi-cache] Applying Deployments..."
kubectl_apply_if_changed -f "$GENERATED_DIR/deployments.yaml"

# --- Restart deployments to pick up ConfigMap changes ---
# subPath volume mounts do not auto-update when the underlying ConfigMap
# changes.  Force a rolling restart so pods always run with the latest
# nginx.conf (e.g. DNS resolver IP discovered at deploy time).
echo "[pypi-cache] Restarting deployments to pick up ConfigMap changes..."
SLUGS=$(uv run "$MODULE_DIR/scripts/python/generate_manifests.py" \
  --cluster "$CLUSTER" \
  --clusters-yaml "$CLUSTERS_YAML" \
  --list-slugs)

for slug in $SLUGS; do
  kubectl rollout restart deployment "pypi-cache-${slug}" -n "$NAMESPACE"
done

# --- Wait for deployment rollouts ---
echo "[pypi-cache] Waiting for rollouts..."

for slug in $SLUGS; do
  echo "[pypi-cache] Waiting for pypi-cache-${slug}..."
  kubectl rollout status deployment "pypi-cache-${slug}" \
    -n "$NAMESPACE" --timeout=300s || {
    echo "[pypi-cache] WARNING: Deployment pypi-cache-${slug} rollout did not complete within timeout"
    echo "[pypi-cache] Check: kubectl get pods -n $NAMESPACE -l cuda-version=${slug}"
  }
done

echo "[pypi-cache] Deployed — EFS-backed Deployments serving per-CUDA package caches."
