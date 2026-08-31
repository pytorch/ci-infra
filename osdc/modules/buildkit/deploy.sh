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

# Read per-installation config. {amd64,arm64}_instance_types map an instance
# type to its pods-per-node; several sizes give Karpenter a capacity fallback.
# pods_per_node stays the default for any entry that omits its count.
REPLICAS=$(uv run "$CFG" "$CLUSTER" buildkit.replicas_per_arch 4)
# No inline fallback: defaults.buildkit in clusters.yaml always supplies these,
# and a missing value should abort rather than silently deploy a guess.
ARM64_INSTANCE=$(uv run "$CFG" "$CLUSTER" buildkit.arm64_instance_types)
AMD64_INSTANCE=$(uv run "$CFG" "$CLUSTER" buildkit.amd64_instance_types)
PODS_PER_NODE=$(uv run "$CFG" "$CLUSTER" buildkit.pods_per_node 2)
AMD64_REPLICAS=$(uv run "$CFG" "$CLUSTER" buildkit.amd64_replicas "$REPLICAS")
ARM64_REPLICAS=$(uv run "$CFG" "$CLUSTER" buildkit.arm64_replicas "$REPLICAS")
# Lowercase via tr (not ${VAR,,}) — deploy.sh runs under macOS bash 3.2 too.
AUTOSCALING=$(uv run "$CFG" "$CLUSTER" buildkit.autoscaling.enabled false | tr '[:upper:]' '[:lower:]')

GENERATED_DIR="$MODULE_DIR/generated"

# --- Generate manifests ---

echo "Generating BuildKit manifests..."
GEN_ARGS=(
  --arm64-instance-type "$ARM64_INSTANCE"
  --amd64-instance-type "$AMD64_INSTANCE"
  --replicas "$REPLICAS"
  --pods-per-node "$PODS_PER_NODE"
  --amd64-replicas "$AMD64_REPLICAS"
  --arm64-replicas "$ARM64_REPLICAS"
  --output-dir "$GENERATED_DIR"
)
if [[ "$AUTOSCALING" == "true" ]]; then
  AMD64_MIN=$(uv run "$CFG" "$CLUSTER" buildkit.autoscaling.amd64_min 2)
  AMD64_MAX=$(uv run "$CFG" "$CLUSTER" buildkit.autoscaling.amd64_max 8)
  ARM64_MIN=$(uv run "$CFG" "$CLUSTER" buildkit.autoscaling.arm64_min 4)
  ARM64_MAX=$(uv run "$CFG" "$CLUSTER" buildkit.autoscaling.arm64_max 8)
  # Fallback replicas if KEDA can't read the scale metric (0 = no fallback).
  AMD64_FALLBACK=$(uv run "$CFG" "$CLUSTER" buildkit.autoscaling.amd64_fallback 0)
  ARM64_FALLBACK=$(uv run "$CFG" "$CLUSTER" buildkit.autoscaling.arm64_fallback 0)
  GEN_ARGS+=(
    --autoscaling
    --amd64-min "$AMD64_MIN"
    --amd64-max "$AMD64_MAX"
    --arm64-min "$ARM64_MIN"
    --arm64-max "$ARM64_MAX"
    --amd64-fallback "$AMD64_FALLBACK"
    --arm64-fallback "$ARM64_FALLBACK"
  )
fi
uv run "$MODULE_DIR/scripts/python/generate_buildkit.py" "${GEN_ARGS[@]}"

# --- Apply NodePools (with cluster name substitution) ---

echo "Applying BuildKit Karpenter NodePools..."
sed "s/CLUSTER_NAME_PLACEHOLDER/$CNAME/g" "$GENERATED_DIR/nodepools.yaml" | kubectl_apply_if_changed -f -

# Pool names are derived from {amd64,arm64}_instance_types, so dropping or
# renaming an entry leaves its NodePool behind. An orphan is not inert: the pod
# selects on workload-type and arch only, so a stale pool keeps provisioning
# nodes of a size no longer configured. Apply never deletes, so sweep by label.
_kind_names() { # kind -> names of that kind in the generated manifest
  awk -v kind="$1" '
    $0 == "kind: " kind { want = 1; next }
    want && /^  name: / { print $2; want = 0 }
  ' "$GENERATED_DIR/nodepools.yaml"
}

_prune_stale() { # resource, kind
  local resource="$1" kind="$2" expected deployed
  expected=$(_kind_names "$kind")
  # Never prune off an empty expected set — that would delete the whole module.
  if [[ -z "$expected" ]]; then
    echo "  WARNING: no $kind found in generated manifest; skipping stale sweep"
    return 0
  fi
  deployed=$(kubectl get "$resource" -l "osdc.io/module=buildkit" \
    -o jsonpath='{.items[*].metadata.name}' 2>/dev/null || echo "")
  for name in $deployed; do
    if ! grep -qxF "$name" <<<"$expected"; then
      echo "  Deleting stale $kind: $name"
      kubectl delete "$resource" "$name" --wait=false 2>/dev/null \
        || echo "    WARNING: failed to delete $kind $name (continuing)"
    fi
  done
}

echo "Checking for stale BuildKit NodePools..."
_prune_stale nodepools.karpenter.sh NodePool
_prune_stale ec2nodeclasses.karpenter.k8s.aws EC2NodeClass

# --- Apply static k8s resources ---

echo "Applying BuildKit static manifests..."
# Stamp the buildkitd-lb pod template with a hash of haproxy.yaml. HAProxy reads
# its config only at container start, and nothing else restarts the LB, so
# without this a ConfigMap change (maxconn, timeouts, backends) would silently
# not take effect until the pod happened to be recreated. Changing the hash
# rolls the Deployment whenever the config changes.
HAPROXY_SUM=$(shasum -a 256 "$MODULE_DIR/kubernetes/base/haproxy.yaml" | cut -c1-12)
kubectl kustomize "$MODULE_DIR/kubernetes/base/" \
  | sed "s/__HAPROXY_CFG_CHECKSUM__/$HAPROXY_SUM/" \
  | kubectl_apply_if_changed -f -

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

# --- KEDA autoscaling (optional) ---
# Scales on the in-cluster buildkit LB metrics; no external metrics backend.

if [[ "$AUTOSCALING" == "true" ]]; then
  echo "Applying KEDA autoscaling manifests..."
  kubectl_apply_if_changed -f "$GENERATED_DIR/autoscaling.yaml"
fi

echo "BuildKit deployed."
kubectl get pods -n buildkit -o wide
