#!/usr/bin/env bash
set -euo pipefail
#
# [TEST-ONLY] Clean up NUMA modules from arc-staging after testing.
# Removes all resources created by the nfd + numa-scheduler test deploy
# and restores the cluster to its pre-test state.
#
# Usage: bash modules/nfd/scripts/cleanup-arc-staging.sh
#
# After running this script, drop the test commit:
#   git reset --hard HEAD~1

CTX="pytorch-arc-staging"

echo "=== Cleaning up NUMA test resources from arc-staging ==="

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

# 3. Restore local files to pre-test state
echo "→ Restoring modified files..."
git checkout -- \
  modules/nfd/helm/values.yaml \
  modules/nfd/kubernetes/nfd-taint-remover.yaml \
  modules/nodepools/scripts/python/generate_nodepools.py \
  modules/nodepools/defs/p4d.yaml \
  modules/arc-runners/defs/l-x86iavx512-11-125-a100.yaml \
  modules/arc-runners/defs/l-x86iavx512-22-250-a100-2.yaml \
  modules/arc-runners/defs/l-x86iavx512-44-500-a100-4.yaml \
  modules/arc-runners/defs/l-bx86iavx512-88-1000-a100-8.yaml \
  clusters.yaml

# 4. Redeploy nodepools + arc-runners to restore original state on cluster
echo "→ Redeploying nodepools (removes p4d startup taint, restores exclude_regions)..."
just deploy-module arc-staging nodepools
echo "→ Redeploying arc-runners (restores original A100 runner defs)..."
just deploy-module arc-staging arc-runners

echo ""
echo "=== Cleanup complete ==="
echo "Now drop the test commit: git reset --hard HEAD~1"
