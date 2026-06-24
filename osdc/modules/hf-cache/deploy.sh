#!/usr/bin/env bash
set -euo pipefail
#
# HF cache module deploy script.
# Called by: just deploy-module <cluster> hf-cache
# Args: $1=cluster-id  $2=cluster-name  $3=region
#
# Deploys:
#   1. hf-cache namespace + ServiceAccounts (kustomize)
#   2. IRSA annotations on the mount/refresh ServiceAccounts (terraform outputs)
#   3. ConfigMaps (refresh script + model manifest)
#   4. Mount DaemonSet (rclone FUSE → read-only /mnt/hf_cache on each runner node)
#   5. Refresh CronJob (downloads curated models → publishes to shared S3 bucket)
#
# The model cache data lives in a single shared S3 bucket (see
# terraform/hf-cache-bucket/). This per-cluster deploy only wires the IRSA roles,
# the node mount, and the refresh job.

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

# --- Read hf_cache config (with defaults) ---
NAMESPACE=$(uv run "$CFG" "$CLUSTER" hf_cache.namespace hf-cache)
# The model-cache bucket is per-region and lives in the cluster's own region, so
# runners read it without cross-region S3 egress. rclone's S3 region therefore
# matches the cluster region (passed as $3).
BUCKET_REGION="$REGION"
RCLONE_IMAGE=$(uv run "$CFG" "$CLUSTER" hf_cache.rclone_image "rclone/rclone:1.69.1")
VFS_CACHE_MAX_SIZE=$(uv run "$CFG" "$CLUSTER" hf_cache.vfs_cache_max_size 200G)
REFRESH_SCHEDULE=$(uv run "$CFG" "$CLUSTER" hf_cache.refresh_schedule "0 7 * * *")
BUCKET_CFG=$(uv run "$CFG" "$CLUSTER" state_bucket)

# --- Read terraform outputs: IRSA role ARNs + bucket name ---
echo "[hf-cache] Reading terraform outputs..."
cd "$MODULE_DIR/terraform"
tofu init -reconfigure \
  -backend-config="bucket=${BUCKET_CFG}" \
  -backend-config="key=${CLUSTER}/hf-cache/terraform.tfstate" \
  -backend-config="region=${STATE_REGION}" \
  -backend-config="dynamodb_table=ciforge-terraform-locks" \
  >/dev/null 2>&1
MOUNT_ROLE_ARN=$(tofu output -raw mount_role_arn)
REFRESH_ROLE_ARN=$(tofu output -raw refresh_role_arn)
BUCKET=$(tofu output -raw hf_cache_bucket)
cd - >/dev/null
echo "[hf-cache] Bucket: ${BUCKET} (${BUCKET_REGION})"
echo "[hf-cache] Mount role ARN: ${MOUNT_ROLE_ARN}"
echo "[hf-cache] Refresh role ARN: ${REFRESH_ROLE_ARN}"

# --- Apply base k8s resources (namespace, ServiceAccounts) ---
echo "[hf-cache] Applying base resources (namespace, ServiceAccounts)..."
kubectl_apply_if_changed -k "$MODULE_DIR/kubernetes/"

echo "[hf-cache] Annotating ServiceAccounts with IRSA roles..."
kubectl annotate sa hf-cache-mount -n "$NAMESPACE" \
  eks.amazonaws.com/role-arn="$MOUNT_ROLE_ARN" --overwrite
kubectl annotate sa hf-cache-refresh -n "$NAMESPACE" \
  eks.amazonaws.com/role-arn="$REFRESH_ROLE_ARN" --overwrite

# --- ConfigMaps (refresh script + model manifest) ---
echo "[hf-cache] Creating hf-cache-refresh-scripts ConfigMap..."
kubectl create configmap hf-cache-refresh-scripts \
  --from-file=refresh_cache.py="$MODULE_DIR/scripts/python/refresh_cache.py" \
  -n "$NAMESPACE" \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl label configmap hf-cache-refresh-scripts -n "$NAMESPACE" \
  osdc.io/module=hf-cache --overwrite

echo "[hf-cache] Creating hf-cache-models ConfigMap..."
kubectl create configmap hf-cache-models \
  --from-file=models.txt="$MODULE_DIR/models.txt" \
  -n "$NAMESPACE" \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl label configmap hf-cache-models -n "$NAMESPACE" \
  osdc.io/module=hf-cache --overwrite

# --- Render + apply the mount DaemonSet ---
echo "[hf-cache] Applying mount DaemonSet..."
sed -e "s|__NAMESPACE__|${NAMESPACE}|g" \
  -e "s|__BUCKET__|${BUCKET}|g" \
  -e "s|__REGION__|${BUCKET_REGION}|g" \
  -e "s|__RCLONE_IMAGE__|${RCLONE_IMAGE}|g" \
  -e "s|__VFS_CACHE_MAX_SIZE__|${VFS_CACHE_MAX_SIZE}|g" \
  "$MODULE_DIR/kubernetes/mount-daemonset.yaml.tpl" \
  | kubectl_apply_if_changed -f -

# --- Render + apply the refresh CronJob ---
echo "[hf-cache] Applying refresh CronJob..."
sed -e "s|__NAMESPACE__|${NAMESPACE}|g" \
  -e "s|__BUCKET__|${BUCKET}|g" \
  -e "s|__REGION__|${BUCKET_REGION}|g" \
  -e "s|__RCLONE_IMAGE__|${RCLONE_IMAGE}|g" \
  -e "s|__SCHEDULE__|${REFRESH_SCHEDULE}|g" \
  "$MODULE_DIR/kubernetes/refresh-cronjob.yaml.tpl" \
  | kubectl_apply_if_changed -f -

# --- Wait for the mount DaemonSet to roll out ---
echo "[hf-cache] Waiting for mount DaemonSet rollout..."
kubectl rollout status daemonset hf-cache-mount \
  -n "$NAMESPACE" --timeout=300s || {
  echo "[hf-cache] WARNING: mount DaemonSet rollout did not complete within timeout"
  echo "[hf-cache] Check: kubectl get pods -n $NAMESPACE -l app=hf-cache-mount"
}

echo "[hf-cache] Deployed — rclone mount serving read-only /mnt/hf_cache on runner nodes."
echo "[hf-cache] To populate now: kubectl create job -n $NAMESPACE --from=cronjob/hf-cache-refresh hf-cache-refresh-manual"
