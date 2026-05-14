#!/usr/bin/env bash
set -euo pipefail
#
# AZ-named ENIConfig deploy script.
# Called from the justfile's deploy-base recipe.
#
# Args: $1=cluster-id
#
# Renders one ENIConfig per AZ from eniconfig.yaml.tpl, pointing at the
# matching primary /18 private subnet from the base terraform output
# `private_subnets_by_az`. Resources are inert until VPC CNI Custom
# Networking is enabled in a later PR.

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
SUBNETS_JSON=$(tofu output -json private_subnets_by_az)
cd - >/dev/null

# macOS ships /bin/bash 3.2, where `mapfile` is unavailable. Read the
# `jq` output line-by-line into the array so the script works on stock
# bash 3.2 and modern bash alike.
AZS=()
while IFS= read -r line; do AZS+=("$line"); done < <(jq -r 'keys[]' <<<"$SUBNETS_JSON")
if [[ ${#AZS[@]} -eq 0 ]]; then
  echo "ERROR: private_subnets_by_az is empty for cluster ${CLUSTER}" >&2
  exit 1
fi

echo "Applying ${#AZS[@]} ENIConfig(s) for cluster ${CLUSTER}..."
for az in "${AZS[@]}"; do
  subnet_id=$(jq -r --arg k "$az" '.[$k]' <<<"$SUBNETS_JSON")
  echo "  ENIConfig ${az} -> ${subnet_id}"
  sed \
    -e "s|__ENICONFIG_NAME__|${az}|g" \
    -e "s|__SUBNET_ID__|${subnet_id}|g" \
    "$SCRIPT_DIR/eniconfig.yaml.tpl" \
    | kubectl_apply_if_changed -f -
done

echo "ENIConfigs deployed (${#AZS[@]} AZs)."
