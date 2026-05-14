#!/usr/bin/env bash
set -euo pipefail
#
# ENIConfig deploy script.
# Called from the justfile's deploy-base recipe.
#
# Args: $1=cluster-id
#
# Applies two sets of ENIConfig CRDs from eniconfig.yaml.tpl:
#   1. AZ-named ENIConfigs (one per AZ) for base nodes, pointing at the
#      matching primary /18 private subnet from the base terraform output
#      `private_subnets_by_az`.
#   2. Bucket-named ENIConfigs (one per bucket-AZ pair, e.g. bucket-1-us-east-2a)
#      for workload nodes, pointing at the matching per-(bucket, AZ) /16 pod
#      subnet from the base terraform output `pod_subnets_by_bucket_az`.
#
# Resources are inert until VPC CNI Custom Networking is enabled
# (AWS_VPC_K8S_CNI_CUSTOM_NETWORK_CFG=true on the aws-node DaemonSet).

CLUSTER="$1"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
OSDC_UPSTREAM="${OSDC_UPSTREAM:-$(cd "$SCRIPT_DIR/../../.." && pwd)}"
# shellcheck source=/dev/null
source "$OSDC_UPSTREAM/scripts/mise-activate.sh"
# shellcheck source=/dev/null
source "$OSDC_UPSTREAM/scripts/kubectl-apply.sh"
# shellcheck source=/dev/null
source "$OSDC_UPSTREAM/scripts/state-config.sh"
: "${STATE_REGION:?state-config.sh did not export STATE_REGION}"
CFG="$OSDC_UPSTREAM/scripts/cluster-config.py"

BUCKET=$(uv run "$CFG" "$CLUSTER" state_bucket)

cd "$OSDC_UPSTREAM/modules/eks/terraform"
INIT_ERR=$(mktemp)
trap 'rm -f "$INIT_ERR"' EXIT
if ! tofu init -reconfigure \
  -backend-config="bucket=${BUCKET}" \
  -backend-config="key=${CLUSTER}/base/terraform.tfstate" \
  -backend-config="region=${STATE_REGION}" \
  -backend-config="dynamodb_table=ciforge-terraform-locks" \
  >/dev/null 2>"$INIT_ERR"; then
  echo "ERROR: tofu init failed for cluster ${CLUSTER}" >&2
  cat "$INIT_ERR" >&2
  exit 1
fi
ALL_OUTPUTS_JSON=$(tofu output -json)
cd - >/dev/null

AZ_SUBNETS_JSON=$(jq -c '(.private_subnets_by_az.value // {})' <<<"$ALL_OUTPUTS_JSON")
BUCKET_SUBNETS_JSON=$(jq -c '(.pod_subnets_by_bucket_az.value // {})' <<<"$ALL_OUTPUTS_JSON")

if ! kubectl get crd eniconfigs.crd.k8s.amazonaws.com >/dev/null 2>&1; then
  echo "ERROR: ENIConfig CRD (eniconfigs.crd.k8s.amazonaws.com) is not installed in cluster ${CLUSTER}." >&2
  echo "       This CRD is installed by the VPC CNI EKS addon. Verify the addon is healthy." >&2
  exit 1
fi

apply_eniconfig() {
  local name="$1" subnet_id="$2"
  echo "  ENIConfig ${name} -> ${subnet_id}"
  sed \
    -e "s|__ENICONFIG_NAME__|${name}|g" \
    -e "s|__SUBNET_ID__|${subnet_id}|g" \
    "$SCRIPT_DIR/eniconfig.yaml.tpl" \
    | kubectl_apply_if_changed -f -
}

mapfile -t AZS < <(jq -r 'keys[]?' <<<"${AZ_SUBNETS_JSON:-{}}")
if [[ ${#AZS[@]} -eq 0 ]]; then
  echo "ERROR: private_subnets_by_az is empty for cluster ${CLUSTER}" >&2
  exit 1
fi

echo "Applying ${#AZS[@]} AZ-named ENIConfig(s) for cluster ${CLUSTER}..."
for az in "${AZS[@]}"; do
  subnet_id=$(jq -r --arg k "$az" '.[$k]' <<<"$AZ_SUBNETS_JSON")
  apply_eniconfig "$az" "$subnet_id"
done

mapfile -t BUCKET_KEYS < <(jq -r 'keys[]?' <<<"${BUCKET_SUBNETS_JSON:-{}}")
if [[ ${#BUCKET_KEYS[@]} -eq 0 ]]; then
  echo "ERROR: pod_subnets_by_bucket_az is empty for cluster ${CLUSTER}." >&2
  echo "       This output is created by the per-(bucket, AZ) /16 pod subnets in modules/eks/terraform/modules/vpc/." >&2
  echo "       Run 'tofu apply' on modules/eks/terraform for this cluster, then re-run." >&2
  exit 1
fi

echo "Applying ${#BUCKET_KEYS[@]} bucket ENIConfig(s) for cluster ${CLUSTER}..."
for key in "${BUCKET_KEYS[@]}"; do
  subnet_id=$(jq -r --arg k "$key" '.[$k].subnet_id' <<<"$BUCKET_SUBNETS_JSON")
  apply_eniconfig "$key" "$subnet_id"
done

echo "ENIConfigs deployed (${#AZS[@]} AZs, ${#BUCKET_KEYS[@]} buckets)."
