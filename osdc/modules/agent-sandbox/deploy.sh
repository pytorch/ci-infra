#!/usr/bin/env bash
set -euo pipefail
#
# agent-sandbox module deploy. Called by: just deploy-module <cluster> agent-sandbox
# Args: $1=cluster-id  $2=cluster-name  $3=region
#
# Deploys the sandbox:
#   1. Builds the agent image and pushes it to the in-cluster Harbor `osdc`
#      project under a content-hash tag (skipped if that tag already exists).
#   2. Reads the sigv4-proxy IRSA role ARN from terraform outputs.
#   3. Applies the namespace, gvisor RuntimeClass, service accounts, sigv4-proxy,
#      the sandbox-agent worker + Service, and the NetworkPolicies. The image,
#      region and Bedrock model are substituted in.
#   4. Annotates the sigv4-proxy SA with the IRSA role and restarts it.
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

# --- Cleanup trap (Harbor port-forward + temp files) ---
PF_PID=""
NETRC_FILE=""
IMAGE_TAR=""
cleanup() {
  [[ -n "$PF_PID" ]] && { kill "$PF_PID" && wait "$PF_PID"; } 2>/dev/null || true
  [[ -n "$NETRC_FILE" ]] && rm -f "$NETRC_FILE" 2>/dev/null || true
  [[ -n "$IMAGE_TAR" ]] && rm -f "$IMAGE_TAR" 2>/dev/null || true
}
trap cleanup EXIT

NAMESPACE=ai-sandbox
BUCKET_CFG=$(uv run "$CFG" "$CLUSTER" state_bucket)
# Bedrock model for /run. Required: without it the agent clones but every task
# returns errors.bedrock="no model configured".
BEDROCK_DEFAULT_MODEL_ID=$(uv run "$CFG" "$CLUSTER" agent_sandbox.default_model_id "")
if [[ -z "$BEDROCK_DEFAULT_MODEL_ID" ]]; then
  echo "[agent-sandbox] ERROR: agent_sandbox.default_model_id not set for $CLUSTER in clusters.yaml" >&2
  exit 1
fi

# --- Build + push the agent image (mirrors modules/zombie-cleanup) ---
# Content-hash tag over the image inputs, so a code change deploys a new immutable
# tag and an unchanged tree skips the build entirely. Tests are excluded — they
# never enter the image, and hashing them would rebuild on test-only edits.
IMAGE="harbor:30002/osdc/ci-agent-sandbox"
TAG=$(find "$MODULE_DIR/agent" \( -name '*.py' -o -name 'Dockerfile' \) \
  ! -name 'test_*' ! -name 'conftest.py' -print0 | sort -z | xargs -0 cat | sha256sum | cut -c1-12)

HARBOR_ADMIN_PW=$(kubectl get secret harbor-admin-password -n harbor-system \
  -o jsonpath='{.data.password}' | base64 -d)
NETRC_FILE=$(mktemp)
chmod 600 "$NETRC_FILE"
cat >"$NETRC_FILE" <<EOF
machine localhost
login admin
password ${HARBOR_ADMIN_PW}
EOF

kubectl port-forward -n harbor-system svc/harbor 8081:80 &
PF_PID=$!
for i in $(seq 1 30); do
  if curl -s -o /dev/null "http://localhost:8081/api/v2.0/health" 2>/dev/null; then
    break
  fi
  if [[ "$i" -eq 30 ]]; then
    echo "[agent-sandbox] ERROR: Harbor port-forward not ready after 30s" >&2
    exit 1
  fi
  sleep 1
done

# Nodes pull anonymously through the harbor:30002 mirror, so the project must be public.
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" \
  -X POST "http://localhost:8081/api/v2.0/projects" \
  --netrc-file "$NETRC_FILE" \
  -H "Content-Type: application/json" \
  -d '{"project_name":"osdc","public":true}')
case "$HTTP_CODE" in
  201) echo "[agent-sandbox] Created Harbor project 'osdc'" ;;
  409) : ;; # already exists
  *) echo "[agent-sandbox] Warning: Harbor project creation returned HTTP $HTTP_CODE" ;;
esac

crane auth login localhost:8081 -u admin -p "$HARBOR_ADMIN_PW" --insecure
if crane manifest "localhost:8081/osdc/ci-agent-sandbox:${TAG}" --insecure >/dev/null 2>&1; then
  echo "[agent-sandbox] Image osdc/ci-agent-sandbox:${TAG} already exists — skipping build."
else
  echo "[agent-sandbox] Building agent image (tag: ${TAG})..."
  # amd64: the ai-sandbox fleet is a single x86 instance type (defs/ai-sandbox.yaml).
  docker build --platform linux/amd64 -t "ci-agent-sandbox:${TAG}" "$MODULE_DIR/agent"
  echo "[agent-sandbox] Pushing image to Harbor..."
  IMAGE_TAR=$(mktemp)
  docker save "ci-agent-sandbox:${TAG}" -o "$IMAGE_TAR"
  crane push "$IMAGE_TAR" "localhost:8081/osdc/ci-agent-sandbox:${TAG}" --insecure
  rm -f "$IMAGE_TAR"
  IMAGE_TAR=""
fi

# `wait` reaps the job so bash doesn't print "Terminated" into the deploy log.
{ kill "$PF_PID" && wait "$PF_PID"; } 2>/dev/null || true
PF_PID=""
AGENT_IMAGE="${IMAGE}:${TAG}"

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
