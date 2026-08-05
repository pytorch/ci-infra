#!/usr/bin/env bash
set -euo pipefail
#
# agent-sandbox module deploy. Called by: just deploy-module <cluster> agent-sandbox
# Args: $1=cluster-id  $2=cluster-name  $3=region
#
# Deploys the standing sandbox infrastructure:
#   1. Reads the sigv4-proxy IRSA role ARN from terraform outputs.
#   2. Applies the namespace, gvisor RuntimeClass, service accounts, both proxies
#      (agent-vault + sigv4-proxy) and the NetworkPolicies.
#   3. Annotates the sigv4-proxy SA with the IRSA role and restarts it.
#
# It does NOT create agent Jobs — those are per-run (see run-agent.sh). It also
# does NOT create the two required Secrets (agent-sandbox-creds, agent-vault-ca);
# those are operator-provided out-of-band (see README). The deploy warns if they
# are missing so the proxies aren't silently broken.

CLUSTER="$1"
export CNAME="$2"
export REGION="$3"
MODULE_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="${OSDC_ROOT:-$(cd "$MODULE_DIR/../.." && pwd)}"
UPSTREAM_ROOT="${OSDC_UPSTREAM:-$REPO_ROOT}"
# shellcheck source=/dev/null
source "$UPSTREAM_ROOT/scripts/mise-activate.sh"
# shellcheck source=/dev/null
source "$UPSTREAM_ROOT/scripts/kubectl-apply.sh"
# shellcheck source=/dev/null
source "$UPSTREAM_ROOT/scripts/state-config.sh"
: "${STATE_REGION:?state-config.sh did not export STATE_REGION}"
CFG="$UPSTREAM_ROOT/scripts/cluster-config.py"

NAMESPACE=ai-sandbox
BUCKET_CFG=$(uv run "$CFG" "$CLUSTER" state_bucket)

# --- Read terraform outputs (sigv4-proxy IRSA role) ---
echo "[agent-sandbox] Reading terraform outputs..."
cd "$MODULE_DIR/terraform"
tofu init -reconfigure \
  -backend-config="bucket=${BUCKET_CFG}" \
  -backend-config="key=${CLUSTER}/agent-sandbox/terraform.tfstate" \
  -backend-config="region=${STATE_REGION}" \
  -backend-config="dynamodb_table=ciforge-terraform-locks" \
  >/dev/null 2>&1
SIGV4_ROLE_ARN=$(tofu output -raw sigv4_proxy_role_arn)
cd - >/dev/null
echo "[agent-sandbox] sigv4-proxy IRSA role: ${SIGV4_ROLE_ARN}"

# --- Apply static manifests ---
echo "[agent-sandbox] Applying base manifests..."
kubectl_apply_if_changed -k "$MODULE_DIR/kubernetes/base/"

# --- Annotate the sigv4-proxy SA with its IRSA role, then restart it ---
kubectl annotate sa sigv4-proxy -n "$NAMESPACE" \
  eks.amazonaws.com/role-arn="$SIGV4_ROLE_ARN" --overwrite
kubectl rollout restart deployment/sigv4-proxy -n "$NAMESPACE"

# --- Warn on missing operator secrets ---
for secret in agent-sandbox-creds agent-vault-ca; do
  if ! kubectl get secret "$secret" -n "$NAMESPACE" >/dev/null 2>&1; then
    echo "[agent-sandbox] WARNING: secret '$secret' not found in $NAMESPACE."
    echo "[agent-sandbox]          The proxies/agent will not work until it exists — see README."
  fi
done

echo "[agent-sandbox] Waiting for proxy rollouts..."
kubectl rollout status deployment/agent-vault -n "$NAMESPACE" --timeout=5m || true
kubectl rollout status deployment/sigv4-proxy -n "$NAMESPACE" --timeout=5m || true

echo "[agent-sandbox] Deployed. Launch a run with:"
echo "    modules/agent-sandbox/run-agent.sh $CLUSTER <repo> <ref> <task> <bedrock-model-id>"
