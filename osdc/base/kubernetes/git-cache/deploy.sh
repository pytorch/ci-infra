#!/usr/bin/env bash
set -euo pipefail
#
# Git cache central deploy script.
# Called from the justfile's deploy-base recipe.
#
# Args: $1=cluster-id
#
# Generates StatefulSet and PDB from .tpl templates using cluster config,
# then applies them alongside static kustomize resources.

CLUSTER="$1"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
OSDC_UPSTREAM="${OSDC_UPSTREAM:-$(cd "$SCRIPT_DIR/../../.." && pwd)}"
# shellcheck source=/dev/null
source "$OSDC_UPSTREAM/scripts/mise-activate.sh"
CFG="$OSDC_UPSTREAM/scripts/cluster-config.py"
CLUSTERS_YAML="${CLUSTERS_YAML:-$OSDC_UPSTREAM/clusters.yaml}"

# --- Read git_cache config (with defaults fallback) ---
REPLICAS=$(uv run "$CFG" "$CLUSTER" git_cache.central_replicas 2)
CPU_REQUEST=$(uv run "$CFG" "$CLUSTER" git_cache.central_cpu_request "2")
CPU_LIMIT=$(uv run "$CFG" "$CLUSTER" git_cache.central_cpu_limit "2")
MEMORY_REQUEST=$(uv run "$CFG" "$CLUSTER" git_cache.central_memory_request "4Gi")
MEMORY_LIMIT=$(uv run "$CFG" "$CLUSTER" git_cache.central_memory_limit "8Gi")
STORAGE_SIZE=$(uv run "$CFG" "$CLUSTER" git_cache.central_storage_size "50Gi")

# PDB minAvailable = max(1, replicas // 2)
MIN_AVAILABLE=$((REPLICAS / 2))
if [[ $MIN_AVAILABLE -lt 1 ]]; then
  MIN_AVAILABLE=1
fi

echo "Git cache central config:"
echo "  Replicas:       $REPLICAS"
echo "  CPU:            $CPU_REQUEST / $CPU_LIMIT"
echo "  Memory:         $MEMORY_REQUEST / $MEMORY_LIMIT"
echo "  Storage:        $STORAGE_SIZE"
echo "  PDB minAvail:   $MIN_AVAILABLE"

# --- Cleanup trap ---
GENERATED_SS=""
GENERATED_PDB=""
cleanup() {
  [[ -n "$GENERATED_SS" ]] && rm -f "$GENERATED_SS" 2>/dev/null || true
  [[ -n "$GENERATED_PDB" ]] && rm -f "$GENERATED_PDB" 2>/dev/null || true
}
trap cleanup EXIT

# --- Generate StatefulSet from template ---
GENERATED_SS=$(mktemp)
sed \
  -e "s|__REPLICAS__|${REPLICAS}|g" \
  -e "s|__CPU_REQUEST__|${CPU_REQUEST}|g" \
  -e "s|__CPU_LIMIT__|${CPU_LIMIT}|g" \
  -e "s|__MEMORY_REQUEST__|${MEMORY_REQUEST}|g" \
  -e "s|__MEMORY_LIMIT__|${MEMORY_LIMIT}|g" \
  -e "s|__STORAGE_SIZE__|${STORAGE_SIZE}|g" \
  "$SCRIPT_DIR/central-statefulset.yaml.tpl" >"$GENERATED_SS"

# --- Generate PDB from template ---
GENERATED_PDB=$(mktemp)
sed \
  -e "s|__MIN_AVAILABLE__|${MIN_AVAILABLE}|g" \
  "$SCRIPT_DIR/central-pdb.yaml.tpl" >"$GENERATED_PDB"

# --- Migration: delete old Deployment if it exists ---
# StatefulSet and Deployment cannot share the same name; the old Deployment
# must be removed before the StatefulSet can be created.
if kubectl get deployment git-cache-central -n kube-system &>/dev/null; then
  echo "Deleting old git-cache-central Deployment (migrating to StatefulSet)..."
  kubectl delete deployment git-cache-central -n kube-system --wait=false
  # Wait briefly for the deletion to register
  sleep 5
fi

# --- Apply static resources (kustomize) ---
echo "Applying static git-cache resources..."
kubectl apply -k "$SCRIPT_DIR/"

# --- Apply generated resources ---
echo "Applying generated StatefulSet..."
kubectl apply -f "$GENERATED_SS"

echo "Applying generated PDB..."
kubectl apply -f "$GENERATED_PDB"

echo "Git cache central deployed (${REPLICAS} replicas)."
