#!/usr/bin/env bash
set -euo pipefail
#
# agent-sandbox module deploy. Called by: just deploy-module <cluster> agent-sandbox
# Args: $1=cluster-id  $2=cluster-name  $3=region
#
# Deploys the sandbox:
#   1. Reads the sandbox-agent IRSA role ARN (read-only Bedrock) from terraform.
#   2. Applies the namespace, gvisor RuntimeClass, service account, the
#      sandbox-agent worker + Service, and the NetworkPolicies. The agent image +
#      region are substituted from clusters.yaml.
#   3. Annotates the sandbox-agent SA with the IRSA role so the agent can call
#      Bedrock directly (no proxy).
#
# No proxies, no operator secrets: the agent clones public repos anonymously and
# calls Bedrock with its own scoped IRSA role.

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
# Bedrock model for /run. Required: without it the agent clones but every task
# returns errors.bedrock="no model configured".
BEDROCK_DEFAULT_MODEL_ID=$(uv run "$CFG" "$CLUSTER" agent_sandbox.default_model_id "")
if [[ -z "$BEDROCK_DEFAULT_MODEL_ID" ]]; then
  echo "[agent-sandbox] ERROR: agent_sandbox.default_model_id not set for $CLUSTER in clusters.yaml" >&2
  exit 1
fi

# --- Read terraform outputs (sandbox-agent Bedrock IRSA role) ---
echo "[agent-sandbox] Reading terraform outputs..."
cd "$MODULE_DIR/terraform"
tofu init -reconfigure \
  -backend-config="bucket=${BUCKET_CFG}" \
  -backend-config="key=${CLUSTER}/agent-sandbox/terraform.tfstate" \
  -backend-config="region=${STATE_REGION}" \
  -backend-config="dynamodb_table=ciforge-terraform-locks" \
  >/dev/null 2>&1
AGENT_ROLE_ARN=$(tofu output -raw agent_role_arn)
cd - >/dev/null
echo "[agent-sandbox] sandbox-agent Bedrock IRSA role: ${AGENT_ROLE_ARN}"

# --- Apply manifests (substitute the agent image + region + model into sandbox-agent) ---
echo "[agent-sandbox] Applying base manifests (agent image: ${AGENT_IMAGE}, default model: ${BEDROCK_DEFAULT_MODEL_ID})..."
kubectl kustomize "$MODULE_DIR/kubernetes/base/" \
  | sed -e "s|__AGENT_IMAGE__|${AGENT_IMAGE}|g" \
    -e "s|__AWS_REGION__|${REGION}|g" \
    -e "s|__BEDROCK_DEFAULT_MODEL_ID__|${BEDROCK_DEFAULT_MODEL_ID}|g" \
  | kubectl_apply_if_changed -f -

# --- Prune the removed credential-proxy resources (idempotent) ---
# The mitmproxy (agent-vault) + aws-sigv4-proxy were dropped from this module;
# kubectl apply won't delete resources no longer in the manifest set, so remove
# them explicitly. Operator-provided secrets (agent-vault-ca, agent-sandbox-creds)
# are left alone — they were created out-of-band and deleting them is the
# operator's call.
echo "[agent-sandbox] Pruning removed proxy resources (if present)..."
kubectl delete deployment,service,serviceaccount agent-vault sigv4-proxy \
  -n "$NAMESPACE" --ignore-not-found
kubectl delete configmap agent-vault-config -n "$NAMESPACE" --ignore-not-found
# Stale NetworkPolicies from the proxy design: the old `default-deny` (now
# `default-deny-ingress`) plus the proxy/agent egress policies. Leaving them would
# keep egress restricted, contradicting the open-egress prototype.
kubectl delete networkpolicy default-deny proxy-ingress proxy-egress sandbox-agent-egress \
  -n "$NAMESPACE" --ignore-not-found

# --- Annotate the sandbox-agent SA with its Bedrock IRSA role ---
# The pod-identity webhook injects the web-identity token from this annotation
# (independent of automountServiceAccountToken: false). Restart so running pods
# pick up a changed role.
kubectl annotate sa sandbox-agent -n "$NAMESPACE" \
  eks.amazonaws.com/role-arn="$AGENT_ROLE_ARN" --overwrite
kubectl rollout restart deployment/sandbox-agent -n "$NAMESPACE"

echo "[agent-sandbox] Waiting for rollout..."
kubectl rollout status deployment/sandbox-agent -n "$NAMESPACE" --timeout=10m || true

echo "[agent-sandbox] Deployed. The sandbox is callable from arc-runners like buildkitd:"
echo "    curl -sf -X POST http://sandbox-agent.ai-sandbox.svc.cluster.local:8080/run \\"
echo "      -d '{\"repo\":\"pytorch/pytorch\",\"ref\":\"main\",\"task\":\"...\"}'"
