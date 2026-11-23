#!/usr/bin/env bash
set -euo pipefail
#
# Windows GPU NodePool module deploy script.
# Called by: just deploy-module <cluster> nodepools-windows
# Args: $1=cluster-id  $2=cluster-name  $3=region
#
# Deploys the Karpenter EC2NodeClass, NodePool, and NodeOverlay for Windows
# GPU nodes, substituting the cluster name, pre-baked AMI ID, amiFamily, and
# node role from the cluster config. Scales to zero when idle via WhenEmpty.

CLUSTER="$1"
CNAME="$2"
export REGION="$3"
MODULE_DIR="$(cd "$(dirname "$0")" && pwd)"
UPSTREAM_ROOT="${OSDC_UPSTREAM:-$(cd "$MODULE_DIR/../.." && pwd)}"
# shellcheck source=/dev/null
source "$UPSTREAM_ROOT/scripts/mise-activate.sh"
# shellcheck source=/dev/null
source "$UPSTREAM_ROOT/scripts/kubectl-apply.sh"

CFG="$UPSTREAM_ROOT/scripts/cluster-config.py"

# The Windows AMI must be pre-baked with the NVIDIA WDDM driver (produced
# out-of-band) and referenced per-cluster via windows.ami_id.
AMI_ID=$(uv run "$CFG" "$CLUSTER" windows.ami_id "")
if [ -z "$AMI_ID" ]; then
  echo "ERROR: windows.ami_id is not set for cluster '$CLUSTER'." >&2
  echo "Set windows.ami_id in clusters.yaml to a pre-baked Windows GPU AMI." >&2
  exit 1
fi

# Derive the Karpenter amiFamily (Windows2022 / Windows2025 / ...) from the EKS
# ami_type (e.g. WINDOWS_FULL_2025_x86_64 -> Windows2025).
AMI_TYPE=$(uv run "$CFG" "$CLUSTER" windows.ami_type "WINDOWS_FULL_2025_x86_64")
AMI_FAMILY=$(printf '%s' "$AMI_TYPE" | sed -E 's/.*_(20[0-9]{2})_.*/Windows\1/')

# Node IAM role follows the base eks module convention: <cluster-name>-node-role.
ROLE_NAME="${CNAME}-node-role"

echo "Deploying Windows GPU NodePool for ${CNAME} (AMI ${AMI_ID}, ${AMI_FAMILY})"

for f in "$MODULE_DIR"/kubernetes/*.yaml; do
  sed -e "s/__CLUSTER_NAME__/${CNAME}/g" \
      -e "s/__AMI_ID__/${AMI_ID}/g" \
      -e "s/__AMI_FAMILY__/${AMI_FAMILY}/g" \
      -e "s/__ROLE_NAME__/${ROLE_NAME}/g" \
      "$f" | kubectl_apply_if_changed -f -
done

echo "Windows GPU NodePool deployed."
