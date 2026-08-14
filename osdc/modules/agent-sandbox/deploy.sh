#!/usr/bin/env bash
set -euo pipefail
#
# agent-sandbox module deploy. Called by: just deploy-module <cluster> agent-sandbox
# Args: $1=cluster-id  $2=cluster-name  $3=region
#
# Deploys the sandbox:
#   1. Reads the sigv4-proxy IRSA role ARN from terraform outputs.
#   2. Applies the namespace, gvisor RuntimeClass, service accounts, sigv4-proxy,
#      the sandbox-agent worker + Service, and the NetworkPolicies. The agent image
#      + region are substituted from clusters.yaml.
#   3. Annotates the sigv4-proxy SA with the IRSA role and restarts it.
#
# The AWS credential lives on the sigv4-proxy pod; the sandbox worker holds none.
# Public repos are cloned directly, so there is no GitHub credential at all.

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
SIGV4_ROLE_ARN=$(tofu output -raw sigv4_proxy_role_arn)
cd - >/dev/null
echo "[agent-sandbox] sigv4-proxy IRSA role: ${SIGV4_ROLE_ARN}"

# --- Apply manifests (substitute the agent image + region + model into sandbox-agent) ---
echo "[agent-sandbox] Applying base manifests (agent image: ${AGENT_IMAGE}, default model: ${BEDROCK_DEFAULT_MODEL_ID})..."
kubectl kustomize "$MODULE_DIR/kubernetes/base/" \
  | sed -e "s|__AGENT_IMAGE__|${AGENT_IMAGE}|g" \
    -e "s|__AWS_REGION__|${REGION}|g" \
    -e "s|__BEDROCK_DEFAULT_MODEL_ID__|${BEDROCK_DEFAULT_MODEL_ID}|g" \
  | kubectl_apply_if_changed -f -

# --- Prune the no-proxy design's NetworkPolicy (idempotent) ---
# The credential-free design's deny-all is `default-deny` (Ingress + Egress); the
# IRSA design used `default-deny-ingress`. Same reason as the annotation below:
# apply can't delete what the manifests no longer contain, so it would linger.
kubectl delete networkpolicy default-deny-ingress -n "$NAMESPACE" --ignore-not-found

# --- Revoke the agent's own AWS identity (idempotent) ---
# A cluster deployed before the proxies existed has an IRSA role annotated on the
# sandbox-agent SA. `kubectl apply` won't remove an annotation it no longer sets,
# so the agent would keep a usable AWS credential and the whole point of the
# proxies would be silently lost. Strip it, then restart so the pod drops the
# injected web-identity token.
if kubectl get sa sandbox-agent -n "$NAMESPACE" \
  -o jsonpath='{.metadata.annotations.eks\.amazonaws\.com/role-arn}' 2>/dev/null | grep -q .; then
  echo "[agent-sandbox] Removing the stale IRSA role from the sandbox-agent SA..."
  kubectl annotate sa sandbox-agent -n "$NAMESPACE" eks.amazonaws.com/role-arn- >/dev/null
  kubectl rollout restart deployment/sandbox-agent -n "$NAMESPACE"
fi

# --- Annotate the sigv4-proxy SA with its IRSA role, then restart it ---
# The AWS credential is bound here, not to the agent: the pod-identity webhook
# injects the web-identity token from this annotation.
kubectl annotate sa sigv4-proxy -n "$NAMESPACE" \
  eks.amazonaws.com/role-arn="$SIGV4_ROLE_ARN" --overwrite
kubectl rollout restart deployment/sigv4-proxy -n "$NAMESPACE"

echo "[agent-sandbox] Waiting for rollouts..."
kubectl rollout status deployment/sigv4-proxy -n "$NAMESPACE" --timeout=5m || true
kubectl rollout status deployment/sandbox-agent -n "$NAMESPACE" --timeout=10m || true

echo "[agent-sandbox] Deployed. The sandbox is callable from arc-runners like buildkitd:"
echo "    curl -sf -X POST http://sandbox-agent.ai-sandbox.svc.cluster.local:8080/run \\"
echo "      -d '{\"repo\":\"pytorch/pytorch\",\"ref\":\"main\",\"task\":\"...\"}'"
