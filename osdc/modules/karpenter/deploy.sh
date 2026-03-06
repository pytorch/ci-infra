#!/usr/bin/env bash
set -euo pipefail
#
# Karpenter module deploy script.
# Called by: just deploy-module <cluster> karpenter
# Args: $1=cluster-id  $2=cluster-name  $3=region
#
# Installs the Karpenter controller Helm chart with IAM role + queue
# from this module's terraform, and cluster endpoint from base terraform.

CLUSTER="$1"
CNAME="$2"
REGION="$3"
MODULE_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$MODULE_DIR/../.." && pwd)"
source "$REPO_ROOT/scripts/mise-activate.sh"
CFG="$REPO_ROOT/scripts/cluster-config.py"

BUCKET=$(uv run "$CFG" "$CLUSTER" state_bucket)
STATE_REGION="us-west-2"

# Read karpenter module terraform outputs
cd "$MODULE_DIR/terraform"
tofu init -reconfigure \
    -backend-config="bucket=${BUCKET}" \
    -backend-config="key=${CLUSTER}/karpenter/terraform.tfstate" \
    -backend-config="region=${STATE_REGION}" \
    -backend-config="dynamodb_table=ciforge-terraform-locks" \
    >/dev/null 2>&1
KARPENTER_ROLE=$(tofu output -raw role_arn)
QUEUE_NAME=$(tofu output -raw queue_name)
cd - >/dev/null

# Read base terraform outputs (cluster endpoint)
cd "$REPO_ROOT/modules/eks/terraform"
tofu init -reconfigure \
    -backend-config="bucket=${BUCKET}" \
    -backend-config="key=${CLUSTER}/base/terraform.tfstate" \
    -backend-config="region=${STATE_REGION}" \
    -backend-config="dynamodb_table=ciforge-terraform-locks" \
    >/dev/null 2>&1
CLUSTER_ENDPOINT=$(tofu output -raw cluster_endpoint)
cd - >/dev/null

# Read per-cluster Karpenter config
KARPENTER_REPLICAS=$(uv run "$CFG" "$CLUSTER" karpenter.replicas 2)
KARPENTER_LOG_LEVEL=$(uv run "$CFG" "$CLUSTER" karpenter.log_level info)
KARPENTER_PDB_ENABLED=$(uv run "$CFG" "$CLUSTER" karpenter.pdb_enabled true)
KARPENTER_PDB_MIN=$(uv run "$CFG" "$CLUSTER" karpenter.pdb_min_available 1)

echo "Installing Karpenter..."
helm upgrade --install karpenter \
    --namespace karpenter \
    --create-namespace \
    -f "$MODULE_DIR/helm/values.yaml" \
    --set serviceAccount.annotations."eks\.amazonaws\.com/role-arn"="${KARPENTER_ROLE}" \
    --set settings.clusterName="${CNAME}" \
    --set settings.clusterEndpoint="${CLUSTER_ENDPOINT}" \
    --set settings.interruptionQueue="${QUEUE_NAME}" \
    --set replicas="${KARPENTER_REPLICAS}" \
    --set logLevel="${KARPENTER_LOG_LEVEL}" \
    --set podDisruptionBudget.enabled="${KARPENTER_PDB_ENABLED}" \
    --set podDisruptionBudget.minAvailable="${KARPENTER_PDB_MIN}" \
    --timeout 10m \
    --wait \
    oci://public.ecr.aws/karpenter/karpenter \
    --version 1.9.0
echo "Karpenter installed."
