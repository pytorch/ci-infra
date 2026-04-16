#!/usr/bin/env bash
set -euo pipefail
#
# Deploy the zombie-cleanup CronJob.
# Called by: just deploy-module <cluster> zombie-cleanup
#
# Args: $1=cluster-id  $2=cluster-name  $3=region
#
# Builds the container image, pushes it to Harbor, and applies the
# RBAC and CronJob manifests with config values substituted.

CLUSTER="$1"
_CNAME="$2"  # unused but required by deploy-module interface
_REGION="$3" # unused but required by deploy-module interface
MODULE_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="${OSDC_ROOT:-$(cd "$MODULE_DIR/../.." && pwd)}"
UPSTREAM_ROOT="${OSDC_UPSTREAM:-$REPO_ROOT}"

# shellcheck source=/dev/null
source "$UPSTREAM_ROOT/scripts/mise-activate.sh"
# shellcheck source=/dev/null
source "$UPSTREAM_ROOT/scripts/kubectl-apply.sh"

CFG="$UPSTREAM_ROOT/scripts/cluster-config.py"

# --- Cleanup trap ---
PF_PID=""
BUILD_CONTEXT=""
NETRC_FILE=""
IMAGE_TAR=""
cleanup() {
  [[ -n "$PF_PID" ]] && kill "$PF_PID" 2>/dev/null || true
  [[ -n "$BUILD_CONTEXT" ]] && rm -rf "$BUILD_CONTEXT" 2>/dev/null || true
  [[ -n "$NETRC_FILE" ]] && rm -f "$NETRC_FILE" 2>/dev/null || true
  [[ -n "$IMAGE_TAR" ]] && rm -f "$IMAGE_TAR" 2>/dev/null || true
}
trap cleanup EXIT

# --- Check if enabled ---
ENABLED=$(uv run "$CFG" "$CLUSTER" zombie_cleanup.enabled "true")
if [[ "$ENABLED" != "true" ]]; then
  echo "Zombie cleanup disabled for cluster $CLUSTER, skipping."
  exit 0
fi

# --- Read cluster-specific config ---
PENDING_MAX_AGE=$(uv run "$CFG" "$CLUSTER" zombie_cleanup.pending_max_age_hours "24")
RUNNING_MAX_AGE=$(uv run "$CFG" "$CLUSTER" zombie_cleanup.running_max_age_hours "12")
DRY_RUN=$(uv run "$CFG" "$CLUSTER" zombie_cleanup.dry_run "false")
PUSHGATEWAY_URL=$(uv run "$CFG" "$CLUSTER" zombie_cleanup.pushgateway_url "http://prometheus-pushgateway.monitoring:9091")

# --- Compute content-based image tag ---
TAG=$(find "$MODULE_DIR/docker" "$MODULE_DIR/scripts/python" \
  \( -name '*.py' -o -name 'Dockerfile' -o -name 'pyproject.toml' \) \
  ! -name 'test_*' -print0 | sort -z | xargs -0 cat | sha256sum | cut -c1-12)

IMAGE="localhost:30002/osdc/zombie-cleanup"

# --- Connect to Harbor ---
HARBOR_ADMIN_PW=$(kubectl get secret harbor-admin-password -n harbor-system \
  -o jsonpath='{.data.password}' | base64 -d)

NETRC_FILE=$(mktemp)
chmod 600 "$NETRC_FILE"
cat >"$NETRC_FILE" <<EOF
machine localhost
login admin
password ${HARBOR_ADMIN_PW}
EOF

kubectl port-forward -n harbor-system svc/harbor 8081:80 &
PF_PID=$!

# Wait for port-forward to be ready
for i in $(seq 1 30); do
  if curl -s -o /dev/null -w "" "http://localhost:8081/api/v2.0/health" 2>/dev/null; then
    break
  fi
  if [ "$i" -eq 30 ]; then
    echo "ERROR: Harbor port-forward not ready after 30s"
    exit 1
  fi
  sleep 1
done

# Create Harbor project "osdc" if it doesn't exist (409 = already exists)
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" \
  -X POST "http://localhost:8081/api/v2.0/projects" \
  --netrc-file "$NETRC_FILE" \
  -H "Content-Type: application/json" \
  -d '{"project_name":"osdc","public":true}')
if [[ "$HTTP_CODE" == "201" ]]; then
  echo "  Created Harbor project 'osdc'"
elif [[ "$HTTP_CODE" == "409" ]]; then
  echo "  Harbor project 'osdc' already exists"
else
  echo "  Warning: Harbor project creation returned HTTP $HTTP_CODE"
fi

# --- Build + push only if image doesn't already exist ---
crane auth login localhost:8081 -u admin -p "$HARBOR_ADMIN_PW" --insecure
if crane manifest "localhost:8081/osdc/zombie-cleanup:${TAG}" --insecure >/dev/null 2>&1; then
  echo "  Image osdc/zombie-cleanup:${TAG} already exists — skipping build."
else
  echo "Building zombie-cleanup image (tag: ${TAG})..."
  BUILD_CONTEXT=$(mktemp -d)
  cp "$MODULE_DIR/docker/Dockerfile" "$BUILD_CONTEXT/"
  cp "$MODULE_DIR/docker/pyproject.toml" "$BUILD_CONTEXT/"
  cp "$MODULE_DIR/scripts/python/"*.py "$BUILD_CONTEXT/"
  # Exclude test files from the build context
  rm -f "$BUILD_CONTEXT/test_"*.py

  docker build --platform linux/amd64 \
    -t "zombie-cleanup:${TAG}" \
    -t "zombie-cleanup:latest" \
    "$BUILD_CONTEXT"

  echo "Pushing image to Harbor..."
  IMAGE_TAR=$(mktemp)
  docker save "zombie-cleanup:${TAG}" -o "$IMAGE_TAR"
  crane push "$IMAGE_TAR" "localhost:8081/osdc/zombie-cleanup:${TAG}" --insecure
  rm -f "$IMAGE_TAR"
fi

# Kill port-forward now that push is done
kill "$PF_PID" 2>/dev/null || true
PF_PID=""

echo "Using ${IMAGE}:${TAG}"

# --- Apply RBAC ---
echo "  Applying RBAC..."
kubectl_apply_if_changed -f "$MODULE_DIR/kubernetes/rbac.yaml"

# --- Apply CronJob with config substitution ---
echo "  Applying CronJob..."
sed \
  -e "s|ZOMBIE_CLEANUP_IMAGE_PLACEHOLDER|${IMAGE}:${TAG}|" \
  -e "s|PENDING_MAX_AGE_PLACEHOLDER|${PENDING_MAX_AGE}|" \
  -e "s|RUNNING_MAX_AGE_PLACEHOLDER|${RUNNING_MAX_AGE}|" \
  -e "s|DRY_RUN_PLACEHOLDER|${DRY_RUN}|" \
  -e "s|PUSHGATEWAY_URL_PLACEHOLDER|${PUSHGATEWAY_URL}|" \
  "$MODULE_DIR/kubernetes/cronjob.yaml" | kubectl_apply_if_changed -f -

echo "  zombie-cleanup deployed."
