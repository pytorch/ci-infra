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
export REGION="$3"
MODULE_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="${OSDC_ROOT:-$(cd "$MODULE_DIR/../.." && pwd)}"
UPSTREAM_ROOT="${OSDC_UPSTREAM:-$REPO_ROOT}"
# shellcheck source=/dev/null
source "$UPSTREAM_ROOT/scripts/mise-activate.sh"
# shellcheck source=/dev/null
source "$UPSTREAM_ROOT/scripts/helm-upgrade.sh"
# shellcheck source=/dev/null
source "$UPSTREAM_ROOT/scripts/state-config.sh"
: "${STATE_REGION:?state-config.sh did not export STATE_REGION}"
CFG="$UPSTREAM_ROOT/scripts/cluster-config.py"

BUCKET=$(uv run "$CFG" "$CLUSTER" state_bucket)

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
cd "$UPSTREAM_ROOT/modules/eks/terraform"
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

KARPENTER_VERSION="1.12.0"

# Helm does not update CRDs on `helm upgrade` — only on initial install.
# CRDs are in a separate chart (karpenter-crd), not the main karpenter chart.
# On first migration, existing CRDs owned by the `karpenter` release must be
# relabeled so `karpenter-crd` can adopt them.
KARPENTER_CRDS=$(kubectl get crds -o name 2>/dev/null \
  | grep -E "karpenter\.(sh|k8s\.aws)" || true)
if [ -n "$KARPENTER_CRDS" ]; then
  for crd in $KARPENTER_CRDS; do
    kubectl label "$crd" app.kubernetes.io/managed-by=Helm --overwrite
    kubectl annotate "$crd" \
      meta.helm.sh/release-name=karpenter-crd \
      meta.helm.sh/release-namespace=karpenter --overwrite
  done
fi

echo "Updating Karpenter CRDs to v${KARPENTER_VERSION}..."
helm upgrade --install karpenter-crd \
  "oci://public.ecr.aws/karpenter/karpenter-crd" \
  --version "${KARPENTER_VERSION}" \
  --namespace karpenter --create-namespace \
  --timeout 5m --wait

echo "Installing Karpenter..."
helm_upgrade_if_changed karpenter karpenter \
  --create-namespace \
  --history-max 3 \
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
  --version "${KARPENTER_VERSION}"
echo "Karpenter installed."
