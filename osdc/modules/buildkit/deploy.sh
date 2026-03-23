#!/usr/bin/env bash
set -euo pipefail
#
# BuildKit module deploy script.
# Called by: just deploy-module <cluster> buildkit
# Args: $1=cluster-id  $2=cluster-name  $3=region
#
# Deploys:
#   1. Generates Deployment + NodePool YAMLs via Python (pod sizes computed from instance type)
#   2. Applies Karpenter NodePools (with cluster name substitution)
#   3. Applies static k8s resources (namespace, configmap, services, networkpolicy)
#   4. Applies generated Deployments
#   5. Waits for rollout

CLUSTER="$1"
CNAME="$2"
export REGION="$3"
MODULE_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="${OSDC_ROOT:-$(cd "$MODULE_DIR/../.." && pwd)}"
UPSTREAM_ROOT="${OSDC_UPSTREAM:-$REPO_ROOT}"
# shellcheck source=/dev/null
source "$UPSTREAM_ROOT/scripts/mise-activate.sh"
# shellcheck source=/dev/null
source "$UPSTREAM_ROOT/scripts/kubectl-apply.sh"
CFG="$UPSTREAM_ROOT/scripts/cluster-config.py"

# Read per-installation config
REPLICAS=$(uv run "$CFG" "$CLUSTER" buildkit.replicas_per_arch 4)
ARM64_INSTANCE=$(uv run "$CFG" "$CLUSTER" buildkit.arm64_instance_type m8gd.24xlarge)
AMD64_INSTANCE=$(uv run "$CFG" "$CLUSTER" buildkit.amd64_instance_type m6id.24xlarge)
PODS_PER_NODE=$(uv run "$CFG" "$CLUSTER" buildkit.pods_per_node 2)

GENERATED_DIR="$MODULE_DIR/generated"

# --- Generate manifests ---

echo "Generating BuildKit manifests..."
uv run "$MODULE_DIR/scripts/python/generate_buildkit.py" \
  --arm64-instance-type "$ARM64_INSTANCE" \
  --amd64-instance-type "$AMD64_INSTANCE" \
  --replicas "$REPLICAS" \
  --pods-per-node "$PODS_PER_NODE" \
  --output-dir "$GENERATED_DIR"

# --- Apply NodePools (with cluster name substitution) ---

echo "Applying BuildKit Karpenter NodePools..."
sed "s/CLUSTER_NAME_PLACEHOLDER/$CNAME/g" "$GENERATED_DIR/nodepools.yaml" | kubectl_apply_if_changed -f -

# --- Apply static k8s resources ---

echo "Applying BuildKit static manifests..."
kubectl_apply_if_changed -k "$MODULE_DIR/kubernetes/base/"

# --- Apply generated Deployments (only if changed) ---

diff_rc=0
kubectl diff -f "$GENERATED_DIR/deployment.yaml" >/dev/null 2>&1 || diff_rc=$?
if [[ $diff_rc -eq 0 ]]; then
  echo "BuildKit Deployments unchanged — skipping apply"
else
  echo "Applying BuildKit Deployments..."
  kubectl apply -f "$GENERATED_DIR/deployment.yaml"

  # --- Unblock stuck rollouts ---
  # When the deployment's nodeSelector changes (e.g., c7gd → m8gd), new pods
  # are Pending (no matching nodes yet) while old pods hold their spots on
  # stale nodes. RollingUpdate won't kill old pods until new ones are Ready,
  # creating a deadlock. Break it by deleting old Running pods so Karpenter
  # can provision the right node types.
  for arch in arm64 amd64; do
    pending=$(kubectl get pods -n buildkit -l "app=buildkitd,arch=${arch}" \
      --field-selector=status.phase=Pending -o name 2>/dev/null | wc -l | tr -d ' ')
    if [[ "$pending" -gt 0 ]]; then
      echo "  buildkitd-${arch} has ${pending} pending pod(s) — deleting old pods to unblock rollout"
      kubectl delete pods -n buildkit -l "app=buildkitd,arch=${arch}" \
        --field-selector=status.phase=Running --wait=false 2>/dev/null || true
    fi
  done

  # --- Wait for rollout ---
  echo "Waiting for buildkitd rollout..."
  kubectl rollout status deployment/buildkitd-arm64 -n buildkit --timeout=15m
  kubectl rollout status deployment/buildkitd-amd64 -n buildkit --timeout=15m
fi

echo "BuildKit deployed."
kubectl get pods -n buildkit -o wide
