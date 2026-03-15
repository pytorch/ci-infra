#!/usr/bin/env bash
set -euo pipefail
#
# ARC Runners module deploy script.
# Called by: just deploy-module <cluster> arc-runners
# Args: $1=cluster-id  $2=cluster-name  $3=region
#
# Requires: 'arc' module must be deployed first (ARC controller).
# Generates ARC runner scale set configs from defs + template,
# then installs each as a Helm release.
#
# Environment (optional — consumers can override to use local defs):
#   ARC_RUNNERS_DEFS_DIR    — directory containing runner definitions
#   ARC_RUNNERS_OUTPUT_DIR  — directory for generated runner configs
#   ARC_RUNNERS_TEMPLATE    — path to runner.yaml.tpl template
#   ARC_RUNNERS_MODULE_NAME — label value for osdc.io/module (default: "arc-runners")

CLUSTER="$1"
export CNAME="$2"
export REGION="$3"
MODULE_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="${OSDC_ROOT:-$(cd "$MODULE_DIR/../.." && pwd)}"
UPSTREAM_ROOT="${OSDC_UPSTREAM:-$REPO_ROOT}"
# shellcheck source=/dev/null
source "$UPSTREAM_ROOT/scripts/mise-activate.sh"
CFG="$UPSTREAM_ROOT/scripts/cluster-config.py"

# Allow consumers to override defs, output, template, and module name
DEFS_DIR="${ARC_RUNNERS_DEFS_DIR:-$MODULE_DIR/defs}"
OUTPUT_DIR="${ARC_RUNNERS_OUTPUT_DIR:-$MODULE_DIR/generated}"
TEMPLATE="${ARC_RUNNERS_TEMPLATE:-$MODULE_DIR/templates/runner.yaml.tpl}"
MODULE_NAME="${ARC_RUNNERS_MODULE_NAME:-arc-runners}"

# --- Preflight: ARC module must be enabled ---
if ! uv run "$CFG" "$CLUSTER" has-module arc; then
  echo "ERROR: arc-runners requires the 'arc' module."
  echo "Cluster '$CLUSTER' does not have 'arc' in its module list."
  echo "If you only need compute provisioning (NodePools), use the 'nodepools' module instead."
  exit 1
fi

# --- Step 1: Generate ARC runner configs ---
echo "Generating ARC runner configs from definitions..."
ARC_RUNNERS_DEFS_DIR="$DEFS_DIR" \
  ARC_RUNNERS_OUTPUT_DIR="$OUTPUT_DIR" \
  ARC_RUNNERS_TEMPLATE="$TEMPLATE" \
  ARC_RUNNERS_MODULE_NAME="$MODULE_NAME" \
  uv run "$MODULE_DIR/scripts/python/generate_runners.py" "$CLUSTER"

# --- Step 2: Validate runner configs (Guaranteed QoS) ---
echo ""
echo "Validating runner configurations..."
ARC_RUNNERS_OUTPUT_DIR="$OUTPUT_DIR" \
  "$MODULE_DIR/scripts/validate-runner-qos.sh"

# --- Step 3: Apply ARC runner scale sets (parallel) ---
MAX_PARALLEL="${ARC_RUNNERS_MAX_PARALLEL:-10}"
echo ""
echo "Deploying ARC runner scale sets (max ${MAX_PARALLEL} parallel)..."

LOGDIR=$(mktemp -d)
PIDS=()
NAMES=()

cleanup() {
  for pid in "${PIDS[@]:-}"; do
    kill "$pid" 2>/dev/null || true
  done
  wait 2>/dev/null || true
  rm -rf "$LOGDIR"
}
trap cleanup EXIT

# Ensure namespace exists before parallel helm installs
kubectl create namespace arc-runners 2>/dev/null || true

deploy_one_runner() {
  local runner_config="$1"
  local runner_name
  runner_name=$(basename "$runner_config" .yaml)
  # Normalize: dots/underscores → dashes (must match ConfigMap naming)
  runner_name="${runner_name//[._]/-}"
  local logfile="$LOGDIR/${runner_name}.log"

  {
    # Apply ConfigMap (second YAML doc — job pod hook template)
    awk '/^---$/,0' "$runner_config" | kubectl apply -f -

    # Install Helm chart (first YAML doc — ARC scale set values)
    local tmpfile="/tmp/${runner_name}-values-$$.yaml"
    awk 'BEGIN{doc=0} /^---$/{doc++} doc==0' "$runner_config" >"$tmpfile"

    helm upgrade --install "arc-${runner_name}" \
      --namespace arc-runners \
      -f "$tmpfile" \
      --set template.spec.securityContext.runAsUser=1000 \
      --set template.spec.securityContext.runAsGroup=1000 \
      --set template.spec.securityContext.fsGroup=1000 \
      oci://ghcr.io/actions/actions-runner-controller-charts/gha-runner-scale-set \
      --version 0.13.1 \
      --wait

    rm -f "$tmpfile"
  } >"$logfile" 2>&1
}

shopt -s nullglob
_generated=("$OUTPUT_DIR/"*.yaml)
shopt -u nullglob
if ((${#_generated[@]} == 0)); then
  echo "ERROR: No generated runner YAML files in $OUTPUT_DIR"
  exit 1
fi

for runner_config in "${_generated[@]}"; do
  runner_name=$(basename "$runner_config" .yaml)
  # Normalize for log file matching (must match deploy_one_runner's normalization)
  runner_name_normalized="${runner_name//[._]/-}"
  echo "  → ${runner_name}"

  deploy_one_runner "$runner_config" &
  PIDS+=($!)
  NAMES+=("$runner_name_normalized")

  # Concurrency limiter: wait for a slot if at max
  while (($(jobs -rp | wc -l) >= MAX_PARALLEL)); do
    sleep 0.5
  done
done

# Wait for all and collect failures
FAILED=()
for i in "${!PIDS[@]}"; do
  if ! wait "${PIDS[$i]}"; then
    FAILED+=("${NAMES[$i]}")
  fi
done

# Print logs for any failures
if ((${#FAILED[@]} > 0)); then
  echo ""
  echo "ERROR: ${#FAILED[@]} runner(s) failed to deploy:"
  for name in "${FAILED[@]}"; do
    echo "  ✗ ${name}"
    sed 's/^/    /' <"$LOGDIR/${name}.log"
  done
  exit 1
fi

echo ""
echo "ARC runners deployed."

# --- Step 4: Clean up stale ARC runners ---
echo ""
echo "Checking for stale ARC runners (module: ${MODULE_NAME})..."

# Build set of expected normalized names from generated files
# Normalize: dots/underscores → dashes (must match ConfigMap naming convention)
EXPECTED_RUNNERS=()
for f in "${_generated[@]}"; do
  raw_name="$(basename "$f" .yaml)"
  EXPECTED_RUNNERS+=("${raw_name//[._]/-}")
done

# Query cluster for ConfigMaps with our module label
DEPLOYED_CMS=$(kubectl get configmaps -n arc-runners \
  -l "osdc.io/module=${MODULE_NAME}" \
  -o jsonpath='{.items[*].metadata.name}' 2>/dev/null || echo "")

STALE_COUNT=0
for cm in $DEPLOYED_CMS; do
  # ConfigMap name format: arc-runner-hook-{normalized_name}
  # Strip prefix to get the normalized runner name
  local_name="${cm#arc-runner-hook-}"

  is_expected=false
  for expected in "${EXPECTED_RUNNERS[@]}"; do
    if [[ "$local_name" == "$expected" ]]; then
      is_expected=true
      break
    fi
  done
  if ! $is_expected; then
    echo "  Deleting stale Helm release: arc-${local_name}"
    helm uninstall "arc-${local_name}" -n arc-runners --wait=false 2>/dev/null \
      || echo "    WARNING: Failed to uninstall Helm release arc-${local_name} (continuing)"
    echo "  Deleting stale ConfigMap: $cm"
    kubectl delete configmap "$cm" -n arc-runners 2>/dev/null \
      || echo "    WARNING: Failed to delete ConfigMap $cm (continuing)"
    STALE_COUNT=$((STALE_COUNT + 1))
  fi
done

if ((STALE_COUNT > 0)); then
  echo "Cleaned up $STALE_COUNT stale runner(s)."
else
  echo "No stale runners found."
fi
