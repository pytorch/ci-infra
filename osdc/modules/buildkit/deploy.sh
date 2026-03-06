#!/usr/bin/env bash
set -euo pipefail
#
# BuildKit module deploy script.
# Called by: just deploy-module <cluster> buildkit
# Args: $1=cluster-id  $2=cluster-name  $3=region
#
# Deploys:
#   1. Karpenter NodePools for buildkit nodes (arm64 + amd64)
#   2. BuildKit Kubernetes resources (namespace, deployments, services, networkpolicy)
#   3. Patches replica count from clusters.yaml

CLUSTER="$1"
CNAME="$2"
REGION="$3"
MODULE_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$MODULE_DIR/../.." && pwd)"
source "$REPO_ROOT/scripts/mise-activate.sh"
CFG="$REPO_ROOT/scripts/cluster-config.py"

# Read per-installation config
REPLICAS=$(uv run "$CFG" "$CLUSTER" buildkit.replicas_per_arch 4)

# --- Expand and apply NodePools ---

expand_nodepool() {
    local arch=$1 instance_type=$2 instance_label=$3 cpu_limit=$4 memory_limit=$5
    local userdata template
    userdata=$(sed 's/^/          /' "$MODULE_DIR/node-setup.sh")
    template=$(cat "$MODULE_DIR/nodepool.yaml.tpl")
    template="${template//__ARCH__/$arch}"
    template="${template//__INSTANCE_TYPE__/$instance_type}"
    template="${template//__INSTANCE_LABEL__/$instance_label}"
    template="${template//__CPU_LIMIT__/$cpu_limit}"
    template="${template//__MEMORY_LIMIT__/$memory_limit}"
    template="${template//CLUSTER_NAME_PLACEHOLDER/$CNAME}"
    template="${template/__LOCAL_USERDATA__/$userdata}"
    printf '%s\n' "$template"
}

echo "Applying BuildKit Karpenter NodePools..."
echo "  → buildkit-arm64 (c7gd.16xlarge)"
expand_nodepool arm64 c7gd.16xlarge c7gd.16xlarge 192 384Gi | kubectl apply -f -
echo "  → buildkit-amd64 (m6id.24xlarge)"
expand_nodepool amd64 m6id.24xlarge m6id.24xlarge 288 1152Gi | kubectl apply -f -

# --- Apply k8s resources ---

echo "Applying BuildKit manifests..."
kubectl apply -k "$MODULE_DIR/kubernetes/base/"

# --- Patch replica count from cluster config ---

echo "Setting replicas to ${REPLICAS} per architecture..."
kubectl scale deployment/buildkitd-arm64 -n buildkit --replicas="${REPLICAS}"
kubectl scale deployment/buildkitd-amd64 -n buildkit --replicas="${REPLICAS}"

# --- Unblock stuck rollouts ---
# When the deployment's nodeSelector changes (e.g., c5d → m6id), new pods
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

echo "BuildKit deployed."
kubectl get pods -n buildkit -o wide
