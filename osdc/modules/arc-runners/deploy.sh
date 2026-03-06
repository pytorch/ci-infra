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

CLUSTER="$1"
CNAME="$2"
REGION="$3"
MODULE_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$MODULE_DIR/../.." && pwd)"
source "$REPO_ROOT/scripts/mise-activate.sh"
CFG="$REPO_ROOT/scripts/cluster-config.py"

# --- Preflight: ARC module must be enabled ---
if ! uv run "$CFG" "$CLUSTER" has-module arc; then
    echo "ERROR: arc-runners requires the 'arc' module."
    echo "Cluster '$CLUSTER' does not have 'arc' in its module list."
    echo "If you only need compute provisioning (NodePools), use the 'nodepools' module instead."
    exit 1
fi

# --- Step 1: Generate ARC runner configs ---
echo "Generating ARC runner configs from definitions..."
uv run "$MODULE_DIR/scripts/python/generate_runners.py" "$CLUSTER"

# --- Step 2: Validate runner configs (Guaranteed QoS) ---
echo ""
echo "Validating runner configurations..."
"$MODULE_DIR/scripts/validate-runner-qos.sh"

# --- Step 3: Apply ARC runner scale sets ---
echo ""
echo "Deploying ARC runner scale sets..."
for runner_config in "$MODULE_DIR/generated/"*.yaml; do
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
