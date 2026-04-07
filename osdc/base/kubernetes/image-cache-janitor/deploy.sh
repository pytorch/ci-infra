#!/usr/bin/env bash
set -euo pipefail
#
# Deploy the Image Cache Janitor DaemonSet.
# Called from the justfile's deploy-base recipe.
#
# Args: $1=cluster-id
#
# Builds the container image (python + nsenter), pushes it to Harbor,
# and applies the kustomized manifests with the image reference substituted.

# shellcheck disable=SC2034  # CLUSTER is part of the deploy.sh interface
CLUSTER="$1"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
UPSTREAM_ROOT="${OSDC_UPSTREAM:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
# shellcheck source=/dev/null
source "$UPSTREAM_ROOT/scripts/kubectl-apply.sh"
# shellcheck source=/dev/null
source "$UPSTREAM_ROOT/scripts/mise-activate.sh"

JANITOR_DIR="$SCRIPT_DIR"

# --- Cleanup trap ---
PF_PID=""
cleanup() {
  [[ -n "$PF_PID" ]] && kill "$PF_PID" 2>/dev/null || true
}
trap cleanup EXIT

# --- Compute content-based image tag ---
# v2: multi-arch (amd64 + arm64). Bump version to invalidate old single-arch images.
TAG=$(printf 'v2\n' | cat - "$JANITOR_DIR/docker/Dockerfile" | sha256sum | cut -c1-12)

IMAGE="localhost:30002/osdc/image-cache-janitor"

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
trap 'rm -f "$NETRC_FILE"; cleanup' EXIT

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
if crane manifest "localhost:8081/osdc/image-cache-janitor:${TAG}" --insecure >/dev/null 2>&1; then
  echo "  Image osdc/image-cache-janitor:${TAG} already exists — skipping build."
else
  # Build for both amd64 and arm64 (DaemonSet runs on Graviton + Intel nodes)
  for ARCH in amd64 arm64; do
    echo "Building image-cache-janitor image (tag: ${TAG}-${ARCH})..."
    docker build --platform "linux/${ARCH}" \
      -t "image-cache-janitor:${TAG}-${ARCH}" \
      "$JANITOR_DIR/docker"

    echo "Pushing ${ARCH} image to Harbor..."
    IMAGE_TAR=$(mktemp)
    docker save "image-cache-janitor:${TAG}-${ARCH}" -o "$IMAGE_TAR"
    crane push "$IMAGE_TAR" "localhost:8081/osdc/image-cache-janitor:${TAG}-${ARCH}" --insecure
    rm -f "$IMAGE_TAR"
  done

  echo "Creating multi-arch manifest..."
  crane index append \
    -t "localhost:8081/osdc/image-cache-janitor:${TAG}" \
    -m "localhost:8081/osdc/image-cache-janitor:${TAG}-amd64" \
    -m "localhost:8081/osdc/image-cache-janitor:${TAG}-arm64" \
    --insecure
fi

# Kill port-forward now that push is done
kill "$PF_PID" 2>/dev/null || true
PF_PID=""
rm -f "$NETRC_FILE"

echo "Using ${IMAGE}:${TAG}"

# --- Apply Kubernetes manifests with image substitution ---
echo "Applying image-cache-janitor manifests..."
kubectl kustomize "$JANITOR_DIR/" \
  | sed "s|IMAGE_CACHE_JANITOR_IMAGE_PLACEHOLDER|${IMAGE}:${TAG}|g" \
  | kubectl_apply_if_changed -f -

echo "Image cache janitor deployed."
