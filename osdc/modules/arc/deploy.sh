#!/usr/bin/env bash
set -euo pipefail
#
# ARC module deploy script.
# Called by: just deploy-module <cluster> arc
# Args: $1=cluster-id  $2=cluster-name  $3=region
#
# Installs the ARC controller Helm chart with per-installation config
# from clusters.yaml.

CLUSTER="$1"
export CNAME="$2"
export REGION="$3"
MODULE_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="${OSDC_ROOT:-$(cd "$MODULE_DIR/../.." && pwd)}"
UPSTREAM_ROOT="${OSDC_UPSTREAM:-$REPO_ROOT}"
# shellcheck source=/dev/null
source "$UPSTREAM_ROOT/scripts/mise-activate.sh"
# shellcheck source=/dev/null
source "$UPSTREAM_ROOT/scripts/helm-upgrade.sh"
CFG="$UPSTREAM_ROOT/scripts/cluster-config.py"

# --- Cleanup trap ---
PF_PID=""
NETRC_FILE=""
cleanup() {
  [[ -n "$PF_PID" ]] && kill "$PF_PID" 2>/dev/null || true
  [[ -n "$NETRC_FILE" ]] && rm -f "$NETRC_FILE" 2>/dev/null || true
}
trap cleanup EXIT

# --- Ensure Harbor project "osdc" exists ---
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

# Kill port-forward now that project creation is done
kill "$PF_PID" 2>/dev/null || true
PF_PID=""

# Apply PriorityClasses for proactive capacity (idempotent)
kubectl apply -f "$MODULE_DIR/kubernetes/priority-classes.yaml"

# Apply RBAC for capacity monitor (idempotent)
kubectl apply -f "$MODULE_DIR/kubernetes/capacity-monitor-rbac.yaml"

# Read per-installation ARC config (with defaults)
ARC_CHART_VERSION=$(uv run "$CFG" "$CLUSTER" arc.chart_version 0.14.1-jeanschmidt.16)
ARC_REPLICAS=$(uv run "$CFG" "$CLUSTER" arc.replica_count 2)
ARC_LOG_LEVEL=$(uv run "$CFG" "$CLUSTER" arc.log_level info)
ARC_CPU_REQ=$(uv run "$CFG" "$CLUSTER" arc.controller_cpu_request 1)
ARC_CPU_LIM=$(uv run "$CFG" "$CLUSTER" arc.controller_cpu_limit 4)
ARC_MEM_REQ=$(uv run "$CFG" "$CLUSTER" arc.controller_memory_request 2Gi)
ARC_MEM_LIM=$(uv run "$CFG" "$CLUSTER" arc.controller_memory_limit 4Gi)
# Image repo/tag default to the fork's published image (set in the chart's
# values.yaml at ghcr.io/jeanschmidt/gha-runner-scale-set-controller, tag =
# chart appVersion). Override only for local dev builds pushed to Harbor; see
# docs/arc-fork-build-deploy.md.
ARC_IMAGE_REPO=$(uv run "$CFG" "$CLUSTER" arc.image_repository "ghcr.io/jeanschmidt/gha-runner-scale-set-controller")
ARC_IMAGE_TAG=$(uv run "$CFG" "$CLUSTER" arc.image_tag "${ARC_CHART_VERSION}")

echo "Installing ARC controller v${ARC_CHART_VERSION} (replicas=${ARC_REPLICAS}, logLevel=${ARC_LOG_LEVEL}, cpu=${ARC_CPU_REQ}/${ARC_CPU_LIM}, mem=${ARC_MEM_REQ}/${ARC_MEM_LIM})..."
helm_upgrade_if_changed arc arc-systems \
  --create-namespace \
  --history-max 3 \
  -f "$MODULE_DIR/helm/arc/values.yaml" \
  --set replicaCount="${ARC_REPLICAS}" \
  --set log.level="${ARC_LOG_LEVEL}" \
  --set resources.requests.cpu="${ARC_CPU_REQ}" \
  --set resources.limits.cpu="${ARC_CPU_LIM}" \
  --set resources.requests.memory="${ARC_MEM_REQ}" \
  --set resources.limits.memory="${ARC_MEM_LIM}" \
  --set image.repository="${ARC_IMAGE_REPO}" \
  --set image.tag="${ARC_IMAGE_TAG}" \
  oci://ghcr.io/jeanschmidt/actions-runner-controller-charts/gha-runner-scale-set-controller \
  --version "${ARC_CHART_VERSION}" \
  --timeout 10m \
  --wait

echo "ARC controller installed."

# --- Check for stale AutoscalingListener images ---
DEPLOYED_IMAGE="${ARC_IMAGE_REPO}:${ARC_IMAGE_TAG}"
LISTENER_IMAGES=$(kubectl get autoscalinglisteners -n arc-systems \
  -o jsonpath='{range .items[*]}{.spec.image}{"\n"}{end}' 2>/dev/null || true)

STALE_COUNT=0
STALE_IMAGE=""
while IFS= read -r img; do
  if [[ -n "$img" && "$img" != "$DEPLOYED_IMAGE" ]]; then
    STALE_COUNT=$((STALE_COUNT + 1))
    STALE_IMAGE="$img"
  fi
done <<<"$LISTENER_IMAGES"

if [[ "$STALE_COUNT" -gt 0 ]]; then
  RESTART="${ARC_RESTART_LISTENERS:-ask}"

  case "$RESTART" in
    yes | YES | true | TRUE)
      echo "  Deleting $STALE_COUNT stale listener(s) (image: $STALE_IMAGE)..."
      kubectl delete autoscalinglisteners -n arc-systems --all
      echo "  Listeners deleted — controller will recreate with $DEPLOYED_IMAGE"
      ;;
    no | NO | false | FALSE)
      echo ""
      echo "###################################################################"
      echo "#"
      echo "#  WARNING: STALE LISTENER IMAGE DETECTED"
      echo "#"
      echo "#  ${STALE_COUNT} listener(s) are still running a different image"
      echo "#  than the controller that was just deployed."
      echo "#"
      echo "#  Controller image:  $DEPLOYED_IMAGE"
      echo "#  Listener image:    $STALE_IMAGE"
      echo "#"
      echo "#  The ARC controller does not automatically propagate image"
      echo "#  changes to existing AutoscalingListener CRs. Listeners will"
      echo "#  continue running the old binary until manually restarted."
      echo "#"
      echo "#  To restart listeners with the new image, run:"
      echo "#"
      echo "#    kubectl delete autoscalinglisteners -n arc-systems --all"
      echo "#"
      echo "#  The controller will immediately recreate them with the"
      echo "#  correct image."
      echo "#"
      echo "###################################################################"
      echo ""
      ;;
    *)
      echo ""
      echo "  Controller image changed: $DEPLOYED_IMAGE"
      echo "  ${STALE_COUNT} listener(s) still running: $STALE_IMAGE"
      echo ""
      read -r -p "  Delete stale listeners so controller recreates them? [y/N] " REPLY
      if [[ "$REPLY" =~ ^[Yy]$ ]]; then
        kubectl delete autoscalinglisteners -n arc-systems --all
        echo "  Listeners deleted — controller will recreate with $DEPLOYED_IMAGE"
      else
        echo "  Skipped. To restart manually:"
        echo "    kubectl delete autoscalinglisteners -n arc-systems --all"
      fi
      ;;
  esac
fi
