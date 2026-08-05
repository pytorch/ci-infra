#!/usr/bin/env bash
set -euo pipefail
#
# agent-sandbox module deploy. Called by: just deploy-module <cluster> agent-sandbox
# Args: $1=cluster-id  $2=cluster-name  $3=region
#
# Deploys the standing sandbox infrastructure:
#   1. Reads the sigv4-proxy IRSA role ARN from terraform outputs.
#   2. Applies the namespace, gvisor RuntimeClass, service accounts, both proxies
#      (agent-vault + sigv4-proxy), the sandbox-agent worker + Service, and the
#      NetworkPolicies. The agent image + region are substituted from clusters.yaml.
#   3. Annotates the sigv4-proxy SA with the IRSA role and restarts it.
#
# It does NOT create the two required Secrets (agent-sandbox-creds, agent-vault-ca);
# those are operator-provided out-of-band (see README). The deploy warns if they
# are missing so the proxies/worker aren't silently broken.

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
AGENT_IMAGE=$(uv run "$CFG" "$CLUSTER" agent_sandbox.agent_image "")
if [[ -z "$AGENT_IMAGE" ]]; then
  echo "[agent-sandbox] ERROR: agent_sandbox.agent_image not set for $CLUSTER in clusters.yaml" >&2
  exit 1
fi

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

# --- Apply manifests (substitute the agent image + region into sandbox-agent) ---
echo "[agent-sandbox] Applying base manifests (agent image: ${AGENT_IMAGE})..."
kubectl kustomize "$MODULE_DIR/kubernetes/base/" \
  | sed -e "s|__AGENT_IMAGE__|${AGENT_IMAGE}|g" -e "s|__AWS_REGION__|${REGION}|g" \
  | kubectl_apply_if_changed -f -

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

echo "[agent-sandbox] Waiting for rollouts..."
kubectl rollout status deployment/agent-vault -n "$NAMESPACE" --timeout=5m || true
kubectl rollout status deployment/sigv4-proxy -n "$NAMESPACE" --timeout=5m || true
kubectl rollout status deployment/sandbox-agent -n "$NAMESPACE" --timeout=10m || true

echo "[agent-sandbox] Deployed. The sandbox is callable from arc-runners like buildkitd:"
echo "    curl -sf -X POST http://sandbox-agent.ai-sandbox.svc.cluster.local:8080/run \\"
echo "      -d '{\"repo\":\"pytorch/pytorch\",\"ref\":\"main\",\"task\":\"...\"}'"
