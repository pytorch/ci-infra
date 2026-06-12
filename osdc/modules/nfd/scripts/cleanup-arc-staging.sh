#!/usr/bin/env bash
set -euo pipefail
#
# [TEST-ONLY] Clean up NUMA test resources from arc-staging (g4dn.metal path).
# Removes everything the nfd + numa-scheduler + g4dn-metal-numa test deploy created
# and restores the cluster to its pre-test state.
#
# Usage: bash modules/nfd/scripts/cleanup-arc-staging.sh
#
# IMPORTANT ordering: this script only deletes the live cluster resources. To
# restore the *config*, drop the test commit FIRST, THEN redeploy from the clean
# base — redeploying while this commit is still checked out would just re-apply
# the test config. (This is the bug the A100 cleanup script had.)

CTX="pytorch-arc-staging"

echo "=== Cleaning up NUMA (g4dn.metal) test resources from arc-staging ==="

# 1. Delete namespaced resources (Helm releases + all pods/services/etc.)
echo "→ Deleting nfd namespace..."
kubectl --context "$CTX" delete namespace nfd --ignore-not-found
echo "→ Deleting numa-scheduler namespace..."
kubectl --context "$CTX" delete namespace numa-scheduler --ignore-not-found

# 2. Delete cluster-scoped resources (not removed by namespace deletion)
echo "→ Removing NRT CRD..."
kubectl --context "$CTX" delete crd noderesourcetopologies.topology.node.k8s.io --ignore-not-found
echo "→ Removing nfd-taint-remover ClusterRole/ClusterRoleBinding..."
kubectl --context "$CTX" delete clusterrole nfd-taint-remover --ignore-not-found
kubectl --context "$CTX" delete clusterrolebinding nfd-taint-remover --ignore-not-found

echo ""
echo "=== Cluster resources removed. Now restore the config (in this order): ==="
echo "  1. Drop the test commit:   git checkout numa-aware-scheduling   # or: git reset --hard HEAD~1"
echo "  2. Redeploy from clean base (removes the g4dn-metal-numa NodePool + restores runners):"
echo "       just deploy-module arc-staging nodepools"
echo "       just deploy-module arc-staging arc-runners"
