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
#   4. Karpenter NodePools (dedicated nodes, r5d.12xlarge by default)
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
INSTANCE_TYPE=$(uv run "$CFG" "$CLUSTER" pypi_cache.instance_type "r5d.12xlarge")
TARGET_PYTHON=$(uv run "$CFG" "$CLUSTER" pypi_cache.python_versions "3.10,3.11,3.12" | tr -d "[]' ")
TARGET_ARCH=$(uv run "$CFG" "$CLUSTER" pypi_cache.target_architectures "x86_64,aarch64" | tr -d "[]' ")
TARGET_MANYLINUX=$(uv run "$CFG" "$CLUSTER" pypi_cache.target_manylinux "2_28")
LOG_MAX_AGE_DAYS=$(uv run "$CFG" "$CLUSTER" pypi_cache.log_max_age_days "30")
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
WANTS_COLLECTOR_ROLE_ARN=$(tofu output -raw wants_collector_role_arn)
WHEEL_SYNCER_ROLE_ARN=$(tofu output -raw wheel_syncer_role_arn)
cd - >/dev/null
echo "[pypi-cache] EFS filesystem ID: ${EFS_FS_ID}"
echo "[pypi-cache] Wants collector role ARN: ${WANTS_COLLECTOR_ROLE_ARN}"
echo "[pypi-cache] Wheel syncer role ARN: ${WHEEL_SYNCER_ROLE_ARN}"

# --- Apply base k8s resources (namespace, SA) ---
echo "[pypi-cache] Applying base resources (namespace, ServiceAccount)..."
kubectl_apply_if_changed -k "$MODULE_DIR/kubernetes/"

echo "[pypi-cache] Annotating wants-collector ServiceAccount..."
kubectl annotate sa pypi-wants-collector -n "$NAMESPACE" \
  eks.amazonaws.com/role-arn="$WANTS_COLLECTOR_ROLE_ARN" --overwrite

echo "[pypi-cache] Annotating wheel-syncer ServiceAccount..."
kubectl annotate sa pypi-wheel-syncer -n "$NAMESPACE" \
  eks.amazonaws.com/role-arn="$WHEEL_SYNCER_ROLE_ARN" --overwrite

# --- Create ConfigMaps (must exist before Deployments reference them) ---

# Look up kube-dns ClusterIP for nginx resolver directive.
# Different EKS clusters use different service CIDRs, so this
# must be discovered at deploy time rather than hardcoded.
DNS_RESOLVER=$(kubectl get svc kube-dns -n kube-system -o jsonpath='{.spec.clusterIP}')
echo "[pypi-cache] kube-dns ClusterIP: ${DNS_RESOLVER}"

# Compute nginx max cache size from instance specs (NVMe or emptyDir fallback)
NGINX_MAX_CACHE_SIZE=$(uv run "$MODULE_DIR/scripts/python/generate_manifests.py" \
  --cluster "$CLUSTER" --clusters-yaml "$CLUSTERS_YAML" \
  --print-nginx-max-cache-size)
echo "[pypi-cache] nginx max_cache_size: ${NGINX_MAX_CACHE_SIZE}"

echo "[pypi-cache] Creating pypi-cache-nginx-config ConfigMap..."
# Write rendered nginx.conf to a temp file so we can use --from-file.
# --from-literal passes content through shell expansion, which mangles
# nginx config content.
NGINX_CONF_TMP=$(mktemp)
trap 'rm -f "$NGINX_CONF_TMP"' EXIT
sed -e "s/__DNS_RESOLVER__/${DNS_RESOLVER}/g" \
  -e "s/__NGINX_MAX_CACHE_SIZE__/${NGINX_MAX_CACHE_SIZE}/g" \
  "$MODULE_DIR/kubernetes/nginx.conf" >"$NGINX_CONF_TMP"

kubectl create configmap pypi-cache-nginx-config \
  --from-file=nginx.conf="$NGINX_CONF_TMP" \
  --from-file=merge_indexes.js="$MODULE_DIR/kubernetes/merge_indexes.js" \
  -n "$NAMESPACE" \
  --dry-run=client -o yaml | kubectl apply -f -

echo "[pypi-cache] Creating pypi-wants-collector-scripts ConfigMap..."
kubectl create configmap pypi-wants-collector-scripts \
  --from-file=wants_collector.py="$MODULE_DIR/scripts/python/wants_collector.py" \
  -n "$NAMESPACE" \
  --dry-run=client -o yaml | kubectl apply -f -

echo "[pypi-cache] Creating pypi-wheel-syncer-scripts ConfigMap..."
kubectl create configmap pypi-wheel-syncer-scripts \
  --from-file=wheel_syncer.py="$MODULE_DIR/scripts/python/wheel_syncer.py" \
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

echo "[pypi-cache] Applying PodDisruptionBudgets..."
kubectl_apply_if_changed -f "$GENERATED_DIR/pdbs.yaml"

echo "[pypi-cache] Applying Deployments..."
kubectl_apply_if_changed -f "$GENERATED_DIR/deployments.yaml"

# --- Compute slugs (used by cache-clear and restart loops) ---
SLUGS=$(uv run "$MODULE_DIR/scripts/python/generate_manifests.py" \
  --cluster "$CLUSTER" \
  --clusters-yaml "$CLUSTERS_YAML" \
  --list-slugs)

# --- Optionally clear nginx cache before restart ---
# PYPI_CACHE_CLEAR=yes  → clear without prompt
# PYPI_CACHE_CLEAR=no   → skip without prompt
# unset + CI=true       → skip (non-interactive environment)
# unset + interactive    → prompt user (default yes, 30s timeout)
if [[ "${PYPI_CACHE_CLEAR:-}" == "yes" ]]; then
  _do_clear=1
elif [[ "${PYPI_CACHE_CLEAR:-}" == "no" ]] || [[ "${CI:-}" == "true" ]]; then
  _do_clear=0
else
  _do_clear=1
  if [ -t 0 ]; then
    read -r -t 30 -p "[pypi-cache] Clear nginx cache before restart? (Y/n, 30s timeout=yes) " _answer || true
    if [[ "${_answer:-}" =~ ^[Nn]$ ]]; then
      _do_clear=0
    fi
  fi
fi

if [[ "$_do_clear" == "1" ]]; then
  echo "[pypi-cache] Clearing nginx cache..."
  for slug in $SLUGS; do
    echo "[pypi-cache]   Clearing cache for pypi-cache-${slug}..."
    kubectl exec "deployment/pypi-cache-${slug}" -n "$NAMESPACE" -c nginx -- \
      find /var/cache/nginx/pypi -type f -delete 2>/dev/null || true
  done
fi

# --- Restart deployments to pick up ConfigMap changes ---
# subPath volume mounts do not auto-update when the underlying ConfigMap
# changes.  Force a rolling restart so pods always run with the latest
# nginx.conf (e.g. DNS resolver IP discovered at deploy time).
echo "[pypi-cache] Restarting deployments to pick up ConfigMap changes..."
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

# --- Deploy wants-collector ---
echo "[pypi-cache] Applying wants-collector deployment..."
sed -e "s/__CLUSTER_ID__/${CLUSTER}/g" \
  -e "s/__NAMESPACE__/${NAMESPACE}/g" \
  -e "s/__TARGET_PYTHON_VERSIONS__/${TARGET_PYTHON}/g" \
  -e "s/__TARGET_ARCHITECTURES__/${TARGET_ARCH}/g" \
  -e "s/__TARGET_MANYLINUX__/${TARGET_MANYLINUX}/g" \
  -e "s/__LOG_MAX_AGE_DAYS__/${LOG_MAX_AGE_DAYS}/g" \
  "$MODULE_DIR/kubernetes/wants-collector-deployment.yaml.tpl" \
  | kubectl_apply_if_changed -f -

echo "[pypi-cache] Restarting wants-collector..."
kubectl rollout restart deployment pypi-wants-collector -n "$NAMESPACE"

echo "[pypi-cache] Waiting for wants-collector rollout..."
kubectl rollout status deployment pypi-wants-collector \
  -n "$NAMESPACE" --timeout=120s || {
  echo "[pypi-cache] WARNING: wants-collector rollout did not complete within timeout"
  echo "[pypi-cache] Check: kubectl get pods -n $NAMESPACE -l app=pypi-wants-collector"
}

# --- Deploy wheel-syncer ---
echo "[pypi-cache] Applying wheel-syncer deployment..."
# SLUGS already computed above — convert space-separated to comma-separated for --slugs arg
CUDA_SLUGS=$(echo "$SLUGS" | tr ' ' ',' | tr '\n' ',' | sed 's/,$//')
sed -e "s/__NAMESPACE__/${NAMESPACE}/g" \
  -e "s/__CUDA_SLUGS__/${CUDA_SLUGS}/g" \
  "$MODULE_DIR/kubernetes/wheel-syncer-deployment.yaml.tpl" \
  | kubectl_apply_if_changed -f -

echo "[pypi-cache] Restarting wheel-syncer..."
kubectl rollout restart deployment pypi-wheel-syncer -n "$NAMESPACE"

echo "[pypi-cache] Waiting for wheel-syncer rollout..."
kubectl rollout status deployment pypi-wheel-syncer \
  -n "$NAMESPACE" --timeout=120s || {
  echo "[pypi-cache] WARNING: wheel-syncer rollout did not complete within timeout"
  echo "[pypi-cache] Check: kubectl get pods -n $NAMESPACE -l app=pypi-wheel-syncer"
}

echo "[pypi-cache] Deployed — EFS-backed Deployments serving per-CUDA package caches."
