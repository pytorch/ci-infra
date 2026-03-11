#!/usr/bin/env bash
set -euo pipefail
#
# Deploy the Node Compactor controller.
# Called from the justfile's deploy-base recipe.
#
# Args: $1=cluster-id
#
# Builds the container image, pushes it to Harbor, and applies the
# kustomized manifests with config values substituted into the deployment.

CLUSTER="$1"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
UPSTREAM_ROOT="${OSDC_UPSTREAM:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
# shellcheck source=/dev/null
source "$UPSTREAM_ROOT/scripts/mise-activate.sh"

CLUSTER_CONFIG="$UPSTREAM_ROOT/scripts/cluster-config.py"
COMPACTOR_DIR="$UPSTREAM_ROOT/base/node-compactor"

# --- Cleanup trap ---
PF_PID=""
BUILD_CONTEXT=""
NETRC_FILE=""
cleanup() {
    [[ -n "$PF_PID" ]] && kill "$PF_PID" 2>/dev/null || true
    [[ -n "$BUILD_CONTEXT" ]] && rm -rf "$BUILD_CONTEXT" 2>/dev/null || true
    [[ -n "$NETRC_FILE" ]] && rm -f "$NETRC_FILE" 2>/dev/null || true
}
trap cleanup EXIT

# --- Read cluster-specific compactor config ---
ENABLED=$(uv run "$CLUSTER_CONFIG" "$CLUSTER" node_compactor.enabled "true")
if [[ "$ENABLED" != "true" ]]; then
    echo "Node compactor disabled for cluster $CLUSTER, skipping."
    exit 0
fi

INTERVAL=$(uv run "$CLUSTER_CONFIG" "$CLUSTER" node_compactor.interval_seconds "20")
MAX_UPTIME=$(uv run "$CLUSTER_CONFIG" "$CLUSTER" node_compactor.max_uptime_hours "48")
DRY_RUN=$(uv run "$CLUSTER_CONFIG" "$CLUSTER" node_compactor.dry_run "false")
MIN_NODES=$(uv run "$CLUSTER_CONFIG" "$CLUSTER" node_compactor.min_nodes "1")

# --- Build container image ---
echo "Building node-compactor image..."

TAG="$(date +%Y%m%d-%H%M%S)"
BUILD_CONTEXT=$(mktemp -d)
cp "$COMPACTOR_DIR/docker/Dockerfile" "$BUILD_CONTEXT/"
cp "$COMPACTOR_DIR/docker/pyproject.toml" "$BUILD_CONTEXT/"
cp "$COMPACTOR_DIR/scripts/python/"*.py "$BUILD_CONTEXT/"
# Exclude test files from the build context
rm -f "$BUILD_CONTEXT/test_"*.py

docker build --platform linux/amd64 \
    -t "node-compactor:${TAG}" \
    -t "node-compactor:latest" \
    "$BUILD_CONTEXT"

# --- Push to Harbor ---
echo "Pushing image to Harbor..."

HARBOR_ADMIN_PW=$(kubectl get secret harbor-admin-password -n harbor-system \
    -o jsonpath='{.data.password}' | base64 -d)

# Create netrc file for credential-safe curl calls
NETRC_FILE=$(mktemp)
chmod 600 "$NETRC_FILE"
cat > "$NETRC_FILE" <<EOF
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

# Push image to Harbor via crane (supports --insecure, avoids docker-login HTTPS issues)
crane auth login localhost:8081 -u admin -p "$HARBOR_ADMIN_PW" --insecure
IMAGE_TAR=$(mktemp)
docker save "node-compactor:${TAG}" -o "$IMAGE_TAR"
crane push "$IMAGE_TAR" "localhost:8081/osdc/node-compactor:${TAG}" --insecure
rm -f "$IMAGE_TAR"

# Kill port-forward now that push is done
kill "$PF_PID" 2>/dev/null || true
PF_PID=""

# Image reference for pods uses NodePort (accessible on every node)
IMAGE="localhost:30002/osdc/node-compactor"
echo "Pushed ${IMAGE}:${TAG}"

# --- Apply Kubernetes manifests with config substitution ---
echo "Applying node-compactor manifests..."
kubectl apply -k "$COMPACTOR_DIR/kubernetes/" --dry-run=client -o yaml \
    | sed \
        -e "s|NODE_COMPACTOR_IMAGE_PLACEHOLDER|${IMAGE}:${TAG}|g" \
        -e "s|COMPACTOR_INTERVAL_PLACEHOLDER|\"${INTERVAL}\"|g" \
        -e "s|COMPACTOR_MAX_UPTIME_HOURS_PLACEHOLDER|\"${MAX_UPTIME}\"|g" \
        -e "s|COMPACTOR_DRY_RUN_PLACEHOLDER|\"${DRY_RUN}\"|g" \
        -e "s|COMPACTOR_MIN_NODES_PLACEHOLDER|\"${MIN_NODES}\"|g" \
    | kubectl apply -f -

echo "Node compactor deployed."
