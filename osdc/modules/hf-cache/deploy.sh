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
# amazonlinux:2023 has python3 and pulls reliably at warmup (matches cache-enforcer).
TAINT_REMOVER_IMAGE=$(uv run "$CFG" "$CLUSTER" hf_cache.taint_remover_image \
  "public.ecr.aws/amazonlinux/amazonlinux:2023")
VFS_CACHE_MAX_SIZE=$(uv run "$CFG" "$CLUSTER" hf_cache.vfs_cache_max_size "75%")
# Only large-GPU nodes pull multi-GB models and OOM rclone, so only the -largegpu
# DaemonSet gets the raised limit; standard nodes keep the modest default. Which GPUs
# count as "large" is the instance-gpu-name set below (comma-separated in config).
RCLONE_MEMORY_LIMIT=$(uv run "$CFG" "$CLUSTER" hf_cache.rclone_memory_limit "1Gi")
RCLONE_MEMORY_LIMIT_LARGEGPU=$(uv run "$CFG" "$CLUSTER" hf_cache.rclone_memory_limit_largegpu "4Gi")
# h100,b200 -> ["h100","b200"] for the node-affinity values list.
LARGE_GPU_NAMES=$(uv run "$CFG" "$CLUSTER" hf_cache.large_gpu_names "h100,b200")
LARGE_GPU_VALUES="[\"${LARGE_GPU_NAMES//,/\",\"}\"]"
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

# --- taint-remover library ConfigMap ---
# ConfigMaps are namespaced, so render the shared base taint_remover.py into this
# namespace for the taint-remover sidecar.
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

# --- Render + apply the mount DaemonSet(s) ---
# One template, two DaemonSets (standard + -largegpu); the __GPU_OP__ affinity keeps
# them mutually exclusive so exactly one rclone mount runs per node.
render_mount_ds() {
  # $1 = DaemonSet name, $2 = gpu affinity operator, $3 = rclone memory limit
  sed -e "s|__NAMESPACE__|${NAMESPACE}|g" \
    -e "s|__BUCKET__|${BUCKET}|g" \
    -e "s|__REGION__|${BUCKET_REGION}|g" \
    -e "s|__RCLONE_IMAGE__|${RCLONE_IMAGE}|g" \
    -e "s|__VFS_CACHE_MAX_SIZE__|${VFS_CACHE_MAX_SIZE}|g" \
    -e "s|__TAINT_REMOVER_IMAGE__|${TAINT_REMOVER_IMAGE}|g" \
    -e "s|__DS_NAME__|${1}|g" \
    -e "s|__GPU_OP__|${2}|g" \
    -e "s|__RCLONE_MEMORY_LIMIT__|${3}|g" \
    -e "s|__LARGE_GPU_NAMES__|${LARGE_GPU_VALUES}|g" \
    "$MODULE_DIR/kubernetes/mount-daemonset.yaml.tpl"
}

echo "[hf-cache] Applying mount DaemonSets (standard + large-GPU)..."
{
  render_mount_ds "hf-cache-mount" "NotIn" "${RCLONE_MEMORY_LIMIT}"
  echo "---"
  render_mount_ds "hf-cache-mount-largegpu" "In" "${RCLONE_MEMORY_LIMIT_LARGEGPU}"
} | kubectl_apply_if_changed -f -

echo "[hf-cache] Waiting for mount DaemonSet rollouts..."
for DS in hf-cache-mount hf-cache-mount-largegpu; do
  kubectl rollout status daemonset "$DS" \
    -n "$NAMESPACE" --timeout=300s || {
    echo "[hf-cache] WARNING: $DS rollout did not complete within timeout"
    echo "[hf-cache] Check: kubectl get pods -n $NAMESPACE -l app=$DS"
  }
done

echo "[hf-cache] Deployed — rclone read-only mount serving /mnt/hf_cache on runner nodes."
echo "[hf-cache] Cache is populated by ci-refresh-hf-cache runs (GitHub OIDC write role)."
