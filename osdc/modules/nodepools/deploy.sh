#!/usr/bin/env bash
set -euo pipefail
#
# NodePools module deploy script.
# Called by: just deploy-module <cluster> nodepools
# Args: $1=cluster-id  $2=cluster-name  $3=region
#
# Generates Karpenter NodePool YAMLs from definitions in defs/,
# then applies them to the cluster.
#
# Environment (optional — consumers can override to use local defs):
#   NODEPOOLS_DEFS_DIR    — directory containing nodepool definitions
#   NODEPOOLS_OUTPUT_DIR  — directory for generated NodePool YAMLs
#   NODEPOOLS_MODULE_NAME — label value for osdc.io/module (default: "nodepools")

CLUSTER="$1"
CNAME="$2"
REGION="$3"
MODULE_DIR="$(cd "$(dirname "$0")" && pwd)"
UPSTREAM_ROOT="${OSDC_UPSTREAM:-$(cd "$MODULE_DIR/../.." && pwd)}"
source "$UPSTREAM_ROOT/scripts/mise-activate.sh"

# Allow consumers to override defs, output, and module name
DEFS_DIR="${NODEPOOLS_DEFS_DIR:-$MODULE_DIR/defs}"
OUTPUT_DIR="${NODEPOOLS_OUTPUT_DIR:-$MODULE_DIR/generated}"
MODULE_NAME="${NODEPOOLS_MODULE_NAME:-nodepools}"

# --- Step 1: Generate NodePools from definitions ---
echo "Generating Karpenter NodePools from definitions..."
NODEPOOLS_DEFS_DIR="$DEFS_DIR" \
NODEPOOLS_OUTPUT_DIR="$OUTPUT_DIR" \
NODEPOOLS_MODULE_NAME="$MODULE_NAME" \
    uv run "$MODULE_DIR/scripts/python/generate_nodepools.py"

# --- Step 2: Apply NodePools (parallel) ---
MAX_PARALLEL="${NODEPOOLS_MAX_PARALLEL:-10}"
echo "Applying Karpenter NodePools (max ${MAX_PARALLEL} parallel)..."

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

shopt -s nullglob
_generated=("$OUTPUT_DIR/"*.yaml)
shopt -u nullglob
if (( ${#_generated[@]} == 0 )); then
    echo "ERROR: No generated NodePool YAML files in $OUTPUT_DIR"
    exit 1
fi

for nodepool in "${_generated[@]}"; do
    name=$(basename "$nodepool")
    echo "  → ${name}"
    (
        sed "s/CLUSTER_NAME_PLACEHOLDER/${CNAME}/g" "$nodepool" \
            | kubectl apply -f - \
            > "$LOGDIR/${name}.log" 2>&1
    ) &
    PIDS+=($!)
    NAMES+=("$name")

    # Concurrency limiter: wait for a slot if at max
    while (( $(jobs -rp | wc -l) >= MAX_PARALLEL )); do
        sleep 0.2
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
if (( ${#FAILED[@]} > 0 )); then
    echo ""
    echo "ERROR: ${#FAILED[@]} NodePool(s) failed to apply:"
    for name in "${FAILED[@]}"; do
        echo "  ✗ ${name}"
        cat "$LOGDIR/${name}.log" | sed 's/^/    /'
    done
    exit 1
fi

echo ""
echo "NodePools deployed."

# --- Step 3: Clean up stale NodePools and EC2NodeClasses ---
echo ""
echo "Checking for stale resources (module: ${MODULE_NAME})..."

# Build set of expected names from generated files
EXPECTED_NAMES=()
for f in "${_generated[@]}"; do
    EXPECTED_NAMES+=("$(basename "$f" .yaml)")
done

# Query cluster for NodePools with our module label
DEPLOYED_NODEPOOLS=$(kubectl get nodepools.karpenter.sh \
    -l "osdc.io/module=${MODULE_NAME}" \
    -o jsonpath='{.items[*].metadata.name}' 2>/dev/null || echo "")

STALE_COUNT=0
for deployed in $DEPLOYED_NODEPOOLS; do
    is_expected=false
    for expected in "${EXPECTED_NAMES[@]}"; do
        if [[ "$deployed" == "$expected" ]]; then
            is_expected=true
            break
        fi
    done
    if ! $is_expected; then
        echo "  Deleting stale NodePool: $deployed"
        kubectl delete nodepool.karpenter.sh "$deployed" --wait=false 2>/dev/null || \
            echo "    WARNING: Failed to delete NodePool $deployed (continuing)"
        STALE_COUNT=$((STALE_COUNT + 1))
    fi
done

# Query cluster for EC2NodeClasses with our module label
DEPLOYED_NODECLASSES=$(kubectl get ec2nodeclasses.karpenter.k8s.aws \
    -l "osdc.io/module=${MODULE_NAME}" \
    -o jsonpath='{.items[*].metadata.name}' 2>/dev/null || echo "")

for deployed in $DEPLOYED_NODECLASSES; do
    is_expected=false
    for expected in "${EXPECTED_NAMES[@]}"; do
        if [[ "$deployed" == "$expected" ]]; then
            is_expected=true
            break
        fi
    done
    if ! $is_expected; then
        echo "  Deleting stale EC2NodeClass: $deployed"
        kubectl delete ec2nodeclass.karpenter.k8s.aws "$deployed" --wait=false 2>/dev/null || \
            echo "    WARNING: Failed to delete EC2NodeClass $deployed (continuing)"
        STALE_COUNT=$((STALE_COUNT + 1))
    fi
done

if (( STALE_COUNT > 0 )); then
    echo "Cleaned up $STALE_COUNT stale resource(s)."
else
    echo "No stale resources found."
fi
