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

CLUSTER="$1"
CNAME="$2"
REGION="$3"
MODULE_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="${OSDC_ROOT:-$(cd "$MODULE_DIR/../.." && pwd)}"
UPSTREAM_ROOT="${OSDC_UPSTREAM:-$REPO_ROOT}"
source "$UPSTREAM_ROOT/scripts/mise-activate.sh"
CFG="$UPSTREAM_ROOT/scripts/cluster-config.py"

# Allow consumers to override defs, output, and template paths
DEFS_DIR="${ARC_RUNNERS_DEFS_DIR:-$MODULE_DIR/defs}"
OUTPUT_DIR="${ARC_RUNNERS_OUTPUT_DIR:-$MODULE_DIR/generated}"
TEMPLATE="${ARC_RUNNERS_TEMPLATE:-$MODULE_DIR/templates/runner.yaml.tpl}"

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
    uv run "$MODULE_DIR/scripts/python/generate_runners.py" "$CLUSTER"

# --- Step 2: Validate runner configs (Guaranteed QoS) ---
echo ""
echo "Validating runner configurations..."
ARC_RUNNERS_OUTPUT_DIR="$OUTPUT_DIR" \
    "$MODULE_DIR/scripts/validate-runner-qos.sh"

# --- Step 3: Apply ARC runner scale sets ---
echo ""
echo "Deploying ARC runner scale sets..."
for runner_config in "$OUTPUT_DIR/"*.yaml; do
    if [[ -f "$runner_config" ]]; then
        runner_name=$(basename "$runner_config" .yaml)
        echo "  → ${runner_name}"

        # Apply ConfigMap (second YAML doc — job pod hook template)
        awk '/^---$/,0' "$runner_config" | kubectl apply -f -

        # Install Helm chart (first YAML doc — ARC scale set values)
        tmpfile="/tmp/${runner_name}-values.yaml"
        awk 'BEGIN{doc=0} /^---$/{doc++} doc==0' "$runner_config" > "$tmpfile"

        helm upgrade --install "arc-${runner_name}" \
            --namespace arc-runners \
            --create-namespace \
            -f "$tmpfile" \
            --set template.spec.securityContext.runAsUser=1000 \
            --set template.spec.securityContext.runAsGroup=1000 \
            --set template.spec.securityContext.fsGroup=1000 \
            oci://ghcr.io/actions/actions-runner-controller-charts/gha-runner-scale-set \
            --version 0.13.1 \
            --wait

        rm -f "$tmpfile"
    fi
done

echo ""
echo "ARC runners deployed."
