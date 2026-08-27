#!/usr/bin/env bash
set -euo pipefail
#
# agent-sandbox module deploy. Called by: just deploy-module <cluster> agent-sandbox
# Args: $1=cluster-id  $2=cluster-name  $3=region
#
# Deploys the sandbox:
#   1. Builds the task and dispatcher images and pushes them to the in-cluster Harbor
#      `osdc` project under content-hash tags (skipped if a tag already exists).
#   2. Reads the sigv4-proxy IRSA role ARN from terraform outputs.
#   3. Applies the namespace, quota, gvisor RuntimeClass, service accounts, dispatcher
#      RBAC, sigv4-proxy, the dispatcher + Service, and the NetworkPolicies. Both image
#      tags, the region, the Bedrock model, the proxy's IRSA role ARN and the cluster's
#      API-server ClusterIP are substituted in.
#
# The AWS credential lives on the sigv4-proxy pod; task pods hold none. Public repos
# are cloned directly, so there is no GitHub credential at all. Task pods are created
# per request by the dispatcher, so there is no standing sandbox Deployment.

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

# --- Build + push the images (mirrors modules/zombie-cleanup) ---
# Content-hash tag per image over its own inputs, so a code change deploys a new
# immutable tag, an unchanged tree skips the build entirely, and editing one image does
# not re-roll the other. Tests are excluded — they never enter an image, and hashing
# them would rebuild on test-only edits.
IMAGE="harbor:30002/osdc/ci-agent-sandbox"
DISPATCHER_IMAGE_REPO="harbor:30002/osdc/ci-agent-sandbox-dispatcher"
_hash_dir() {
  find "$1" \( -name '*.py' -o -name 'Dockerfile' \) \
    ! -name 'test_*' ! -name 'conftest.py' -print0 | sort -z | xargs -0 cat | sha256sum | cut -c1-12
}
TAG=$(_hash_dir "$MODULE_DIR/agent")
DISPATCHER_TAG=$(_hash_dir "$MODULE_DIR/dispatcher")

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
build_and_push() {
  local name="$1" tag="$2" context="$3"
  if crane manifest "localhost:8081/osdc/${name}:${tag}" --insecure >/dev/null 2>&1; then
    echo "[agent-sandbox] Image osdc/${name}:${tag} already exists — skipping build."
    return 0
  fi
  echo "[agent-sandbox] Building ${name} image (tag: ${tag})..."
  # amd64: the ai-sandbox fleet is a single x86 instance type (defs/ai-sandbox.yaml),
  # and the dispatcher runs on the x86 base nodes.
  docker build --platform linux/amd64 -t "${name}:${tag}" "$context"
  echo "[agent-sandbox] Pushing ${name} to Harbor..."
  IMAGE_TAR=$(mktemp)
  docker save "${name}:${tag}" -o "$IMAGE_TAR"
  crane push "$IMAGE_TAR" "localhost:8081/osdc/${name}:${tag}" --insecure
  rm -f "$IMAGE_TAR"
  IMAGE_TAR=""
}
build_and_push ci-agent-sandbox "$TAG" "$MODULE_DIR/agent"
build_and_push ci-agent-sandbox-dispatcher "$DISPATCHER_TAG" "$MODULE_DIR/dispatcher"

# `wait` reaps the job so bash doesn't print "Terminated" into the deploy log.
{ kill "$PF_PID" && wait "$PF_PID"; } 2>/dev/null || true
PF_PID=""
AGENT_IMAGE="${IMAGE}:${TAG}"
DISPATCHER_IMAGE="${DISPATCHER_IMAGE_REPO}:${DISPATCHER_TAG}"

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

# --- API server address for the dispatcher's egress policy ---
# Pods reach the API server through the `kubernetes` Service ClusterIP, so the egress
# rule has to name that address; it differs per cluster and is IPv6 here.
K8S_API_IP=$(kubectl get svc kubernetes -n default -o jsonpath='{.spec.clusterIP}')
if [[ -z "$K8S_API_IP" ]]; then
  echo "[agent-sandbox] ERROR: could not read the kubernetes Service ClusterIP" >&2
  exit 1
fi
if [[ "$K8S_API_IP" == *:* ]]; then
  K8S_API_CIDR="${K8S_API_IP}/128"
else
  K8S_API_CIDR="${K8S_API_IP}/32"
fi

# --- Apply manifests (substitute both images, region, model, role ARN, API CIDR) ---
echo "[agent-sandbox] Applying base manifests (task image: ${AGENT_IMAGE}, dispatcher: ${DISPATCHER_IMAGE}, default model: ${BEDROCK_DEFAULT_MODEL_ID})..."
kubectl kustomize "$MODULE_DIR/kubernetes/base/" \
  | sed -e "s|__AGENT_IMAGE__|${AGENT_IMAGE}|g" \
    -e "s|__DISPATCHER_IMAGE__|${DISPATCHER_IMAGE}|g" \
    -e "s|__AWS_REGION__|${REGION}|g" \
    -e "s|__BEDROCK_DEFAULT_MODEL_ID__|${BEDROCK_DEFAULT_MODEL_ID}|g" \
    -e "s|__SIGV4_ROLE_ARN__|${SIGV4_ROLE_ARN}|g" \
    -e "s|__K8S_API_CIDR__|${K8S_API_CIDR}|g" \
  | kubectl_apply_if_changed -f -

# --- Prune objects earlier designs left behind (idempotent) ---
# `kubectl apply` never deletes what the manifests stop containing, so anything dropped
# from kubernetes/base/ keeps running until it is deleted by name. This has already bitten
# once: a Service removed from the manifests survived for two weeks and kept the smoke
# tests green on the one cluster that had it.
#
#   default-deny-ingress   — the IRSA design's deny-all, replaced by `default-deny`.
#   sandbox-agent (Deploy) — the warm serial worker, replaced by one Job per request.
#                            The Service of the same name stays: it now points at the
#                            dispatcher, so callers keep their address.
#   sandbox-agent-egress   — the warm worker's egress, replaced by sandbox-task-egress.
kubectl delete networkpolicy default-deny-ingress -n "$NAMESPACE" --ignore-not-found
kubectl delete networkpolicy sandbox-agent-egress -n "$NAMESPACE" --ignore-not-found
kubectl delete deployment sandbox-agent -n "$NAMESPACE" --ignore-not-found

# --- Revoke the sandbox's own AWS identity (idempotent) ---
# A cluster deployed before the proxies existed has an IRSA role annotated on the
# sandbox-agent SA. `kubectl apply` won't remove an annotation it no longer sets, so
# every task pod created with that SA would get an injected web-identity token and the
# whole point of the proxy would be silently lost. No restart to force: task pods are
# created per request and pick up the SA as it stands when the Job is admitted.
if kubectl get sa sandbox-agent -n "$NAMESPACE" \
  -o jsonpath='{.metadata.annotations.eks\.amazonaws\.com/role-arn}' 2>/dev/null | grep -q .; then
  echo "[agent-sandbox] Removing the stale IRSA role from the sandbox-agent SA..."
  kubectl annotate sa sandbox-agent -n "$NAMESPACE" eks.amazonaws.com/role-arn- >/dev/null
fi

# The sigv4-proxy SA carries its IRSA role from the manifest (substituted above), so
# there is nothing to annotate and no restart to force: the webhook injects the token
# when the pod is admitted, and a token-less proxy window cannot open.

# Not `|| true`: these are the only commands that verify the deploy worked, and the step
# above them removes a credential. A crash-looping new pod leaves the old one serving,
# and swallowing the failure tells the operator it succeeded. There is no task rollout to
# wait for — task pods only exist while a request is in flight.
echo "[agent-sandbox] Waiting for rollouts..."
kubectl rollout status deployment/sigv4-proxy -n "$NAMESPACE" --timeout=5m
kubectl rollout status deployment/sandbox-dispatcher -n "$NAMESPACE" --timeout=10m

echo "[agent-sandbox] Deployed. The sandbox is callable from arc-runners like buildkitd;"
echo "each call runs in its own gVisor pod, up to the namespace quota:"
echo "    curl -sf -m 900 -X POST http://sandbox-agent.ai-sandbox.svc.cluster.local:8080/run \\"
echo "      -d '{\"repo\":\"pytorch/pytorch\",\"ref\":\"main\",\"task\":\"...\"}'"
echo "  or, without holding the connection open:"
echo "    curl -sf -X POST .../run -d '{\"repo\":\"...\",\"wait\":false}'   # -> {\"task_id\": ...}"
echo "    curl -sf .../status/<task_id>"
