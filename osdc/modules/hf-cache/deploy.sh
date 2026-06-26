#!/usr/bin/env bash
set -euo pipefail
#
# HF cache module deploy.  Called by: just deploy-module <cluster> hf-cache
# Args: $1=cluster-id  $2=cluster-name  $3=region
#
# Deploys the hf-cache namespace + ServiceAccount (kustomize), annotates the SA
# with the IRSA role, and rolls out the rclone mount DaemonSet. The per-cluster
# bucket + role are provisioned by terraform (deploy-module phase 1).

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
# shellcheck source=/dev/null
source "$UPSTREAM_ROOT/scripts/state-config.sh"
: "${STATE_REGION:?state-config.sh did not export STATE_REGION}"
CFG="$UPSTREAM_ROOT/scripts/cluster-config.py"

# --- Read hf_cache config ---
NAMESPACE=$(uv run "$CFG" "$CLUSTER" hf_cache.namespace hf-cache)
RCLONE_IMAGE=$(uv run "$CFG" "$CLUSTER" hf_cache.rclone_image "rclone/rclone:1.69.1")
# python3-capable image for the taint-remover sidecar (amazonlinux:2023 ships
# python3 and matches cache-enforcer, which pulls reliably during node warmup).
TAINT_REMOVER_IMAGE=$(uv run "$CFG" "$CLUSTER" hf_cache.taint_remover_image \
  "public.ecr.aws/amazonlinux/amazonlinux:2023")
VFS_CACHE_MAX_SIZE=$(uv run "$CFG" "$CLUSTER" hf_cache.vfs_cache_max_size "75%")
BUCKET_CFG=$(uv run "$CFG" "$CLUSTER" state_bucket)
# Bucket is in the cluster's region, so rclone's S3 region is the cluster region.
BUCKET_REGION="$REGION"

# --- Read terraform outputs ---
echo "[hf-cache] Reading terraform outputs..."
cd "$MODULE_DIR/terraform"
tofu init -reconfigure \
  -backend-config="bucket=${BUCKET_CFG}" \
  -backend-config="key=${CLUSTER}/hf-cache/terraform.tfstate" \
  -backend-config="region=${STATE_REGION}" \
  -backend-config="dynamodb_table=ciforge-terraform-locks" \
  >/dev/null 2>&1
ROLE_ARN=$(tofu output -raw role_arn)
BUCKET=$(tofu output -raw hf_cache_bucket)
cd - >/dev/null
echo "[hf-cache] Bucket: ${BUCKET} (${BUCKET_REGION}); role: ${ROLE_ARN}"

# --- Namespace + ServiceAccount + RBAC, IRSA annotation ---
echo "[hf-cache] Applying base resources..."
kubectl_apply_if_changed -k "$MODULE_DIR/kubernetes/"
kubectl annotate sa hf-cache-mount -n "$NAMESPACE" \
  eks.amazonaws.com/role-arn="$ROLE_ARN" --overwrite

# --- taint-remover library ConfigMap (rendered from the shared base lib) ---
# The DaemonSet's taint-remover sidecar mounts this to clear its startup taint.
# ConfigMaps are namespaced, so render a copy into this module's namespace from
# the same source file the base node-taint-remover uses (no drift).
echo "[hf-cache] Rendering node-taint-remover-lib ConfigMap..."
TAINT_LIB="$UPSTREAM_ROOT/base/kubernetes/node-taint-remover/lib/taint_remover.py"
[[ -f "$TAINT_LIB" ]] || {
  echo "[hf-cache] ERROR: missing $TAINT_LIB" >&2
  exit 1
}
kubectl create configmap node-taint-remover-lib \
  --from-file="taint_remover.py=$TAINT_LIB" \
  -n "$NAMESPACE" --dry-run=client -o yaml \
  | kubectl_apply_if_changed -f -

# --- Render + apply the mount DaemonSet ---
echo "[hf-cache] Applying mount DaemonSet..."
sed -e "s|__NAMESPACE__|${NAMESPACE}|g" \
  -e "s|__BUCKET__|${BUCKET}|g" \
  -e "s|__REGION__|${BUCKET_REGION}|g" \
  -e "s|__RCLONE_IMAGE__|${RCLONE_IMAGE}|g" \
  -e "s|__VFS_CACHE_MAX_SIZE__|${VFS_CACHE_MAX_SIZE}|g" \
  -e "s|__TAINT_REMOVER_IMAGE__|${TAINT_REMOVER_IMAGE}|g" \
  "$MODULE_DIR/kubernetes/mount-daemonset.yaml.tpl" \
  | kubectl_apply_if_changed -f -

echo "[hf-cache] Waiting for mount DaemonSet rollout..."
kubectl rollout status daemonset hf-cache-mount \
  -n "$NAMESPACE" --timeout=300s || {
  echo "[hf-cache] WARNING: mount DaemonSet rollout did not complete within timeout"
  echo "[hf-cache] Check: kubectl get pods -n $NAMESPACE -l app=hf-cache-mount"
}

echo "[hf-cache] Deployed — rclone read-only mount serving /mnt/hf_cache on runner nodes."
echo "[hf-cache] Cache is populated by ci-refresh-hf-cache runs (GitHub OIDC write role)."
