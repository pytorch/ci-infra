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
# rclone RSS scales with job concurrency ~ GPU count, so memory is tiered by
# instance-gpu-count and reserved (request == limit). One DaemonSet per tier; the
# affinity keeps them exclusive. Fields:
#   <ds-name> <affinity-op> <gpu-count-csv> <memory> <buffer-size>
# The "hf-cache-mount" NotIn catch-all covers CPU + any unclassified count, so every
# node gets a mount to clear the startup taint.
# <buffer-size> is rclone's per-open-file read-ahead; rclone RAM ~= buffer-size x
# concurrent open files. The two tight tiers run 0 (read-ahead off; reads served from
# the vfs-cache-mode full on-disk cache): the packed CPU catch-all (48xl/metal, many
# runner pods on one mount) and the 640Mi 1-GPU tier, where even 1M x ~146 shards
# (~146MB) eats headroom rclone's heap/metadata needs. (1-GPU is 640Mi not 1Gi: 1Gi
# strands the a10g/l4/t4 runners on the 8xl 1-GPU nodes; 640Mi still fits per
# analyze_node_utilization.) The roomier multi-GPU tiers (1-4Gi) keep 4M for prefetch.
# FOLLOW-UP: the GPU-only PR drops CPU (nvidia.com/gpu nodeSelector); then collapse the
# first two rows into "hf-cache-mount NotIn 2,4,8 640Mi" (1-GPU + rest, both 640Mi).
MOUNT_TIERS=(
  "hf-cache-mount NotIn 1,2,4,8 640Mi 0"
  "hf-cache-mount-gpu1 In 1 640Mi 0"
  "hf-cache-mount-gpu2 In 2 1Gi 4M"
  "hf-cache-mount-gpu4 In 4 2Gi 4M"
  "hf-cache-mount-gpu8 In 8 4Gi 4M"
)
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

# --- Render + apply the mount DaemonSets (one per GPU-count tier) ---
render_mount_ds() {
  # $1 = DaemonSet name, $2 = affinity operator, $3 = gpu-count CSV,
  # $4 = rclone memory (rendered as both request and limit → reserved),
  # $5 = rclone --buffer-size (per-open-file read-ahead; 0 = off)
  local values="[\"${3//,/\",\"}\"]"
  # GOMEMLIMIT ~= 90% of the reserved limit, in Go's byte-suffix format (MiB/GiB — Go
  # rejects k8s's Mi/Gi). rclone is a Go binary that OOMs from lazy GC, not real need;
  # a soft heap ceiling below the cgroup limit makes the GC run before the kernel kills
  # the mount. Computed here (bash) so it renders to a literal — no runtime arithmetic.
  local mib
  case "$4" in
    *Gi) mib=$((${4%Gi} * 1024)) ;;
    *Mi) mib=${4%Mi} ;;
    *) mib=0 ;;
  esac
  local gomemlimit="$((mib * 90 / 100))MiB"
  sed -e "s|__NAMESPACE__|${NAMESPACE}|g" \
    -e "s|__BUCKET__|${BUCKET}|g" \
    -e "s|__REGION__|${BUCKET_REGION}|g" \
    -e "s|__RCLONE_IMAGE__|${RCLONE_IMAGE}|g" \
    -e "s|__VFS_CACHE_MAX_SIZE__|${VFS_CACHE_MAX_SIZE}|g" \
    -e "s|__TAINT_REMOVER_IMAGE__|${TAINT_REMOVER_IMAGE}|g" \
    -e "s|__DS_NAME__|${1}|g" \
    -e "s|__GPU_OP__|${2}|g" \
    -e "s|__MULTI_GPU_COUNTS__|${values}|g" \
    -e "s|__RCLONE_MEMORY_LIMIT__|${4}|g" \
    -e "s|__GOMEMLIMIT__|${gomemlimit}|g" \
    -e "s|__BUFFER_SIZE__|${5}|g" \
    "$MODULE_DIR/kubernetes/mount-daemonset.yaml.tpl"
}

# Retire the pre-tier -largegpu DaemonSet first: the gpu8 tier supersedes it and
# both would otherwise select 8-GPU nodes and double-mount /mnt/hf_cache.
kubectl delete daemonset hf-cache-mount-largegpu -n "$NAMESPACE" --ignore-not-found

echo "[hf-cache] Applying mount DaemonSets (per GPU-count tier)..."
{
  first=1
  for tier in "${MOUNT_TIERS[@]}"; do
    read -r _name _op _counts _mem _buf <<<"$tier"
    [ "$first" = 1 ] || echo "---"
    first=0
    render_mount_ds "$_name" "$_op" "$_counts" "$_mem" "$_buf"
  done
} | kubectl_apply_if_changed -f -

echo "[hf-cache] Waiting for mount DaemonSet rollouts..."
for tier in "${MOUNT_TIERS[@]}"; do
  read -r _name _rest <<<"$tier"
  kubectl rollout status daemonset "$_name" \
    -n "$NAMESPACE" --timeout=300s || {
    echo "[hf-cache] WARNING: $_name rollout did not complete within timeout"
    echo "[hf-cache] Check: kubectl get pods -n $NAMESPACE -l app=$_name"
  }
done

echo "[hf-cache] Deployed — rclone read-only mount serving /mnt/hf_cache on runner nodes."
echo "[hf-cache] Cache is populated by ci-refresh-hf-cache runs (GitHub OIDC write role)."
