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

CLUSTER="$1"
CNAME="$2"
REGION="$3"
MODULE_DIR="$(cd "$(dirname "$0")" && pwd)"
UPSTREAM_ROOT="${OSDC_UPSTREAM:-$(cd "$MODULE_DIR/../.." && pwd)}"
source "$UPSTREAM_ROOT/scripts/mise-activate.sh"

# Allow consumers to override defs and output directories
DEFS_DIR="${NODEPOOLS_DEFS_DIR:-$MODULE_DIR/defs}"
OUTPUT_DIR="${NODEPOOLS_OUTPUT_DIR:-$MODULE_DIR/generated}"

# --- Step 1: Generate NodePools from definitions ---
echo "Generating Karpenter NodePools from definitions..."
NODEPOOLS_DEFS_DIR="$DEFS_DIR" \
NODEPOOLS_OUTPUT_DIR="$OUTPUT_DIR" \
    uv run "$MODULE_DIR/scripts/python/generate_nodepools.py"

# --- Step 2: Apply NodePools ---
echo "Applying Karpenter NodePools..."
for nodepool in "$OUTPUT_DIR/"*.yaml; do
    if [[ -f "$nodepool" ]]; then
        echo "  → $(basename "$nodepool")"
        sed "s/CLUSTER_NAME_PLACEHOLDER/${CNAME}/g" "$nodepool" | kubectl apply -f -
    fi
done

echo ""
echo "NodePools deployed."
