# PodDisruptionBudgets for Harbor multi-replica components.
#
# The upstream Harbor Helm chart (v1.18.2) does not generate PDBs. This
# template is sed-substituted (__MAX_UNAVAILABLE__) and applied by the
# _deploy-harbor recipe in the justfile via kubectl_apply_if_changed.
#
# Per-cluster value: clusters.yaml -> harbor.pdb_max_unavailable
#   default:     1       (conservative — at most 1 pod down per component)
#   meta-staging-aws-uw1: "100%"  (aggressive — staging has 1 replica each)
#
# Single-replica components (jobservice, portal, exporter, internal redis/db)
# are intentionally NOT covered: a PDB on a 1-replica Deployment with
# minAvailable=1 deadlocks node drains.
#
# Selectors mirror the labels rendered by the Harbor chart's per-component
# Deployments: app=harbor, component=<name>, release=harbor.
---
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: harbor-core
  namespace: harbor-system
spec:
  maxUnavailable: __MAX_UNAVAILABLE__
  selector:
    matchLabels:
      app: harbor
      component: core
      release: harbor
---
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: harbor-registry
  namespace: harbor-system
spec:
  maxUnavailable: __MAX_UNAVAILABLE__
  selector:
    matchLabels:
      app: harbor
      component: registry
      release: harbor
---
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: harbor-nginx
  namespace: harbor-system
spec:
  maxUnavailable: __MAX_UNAVAILABLE__
  selector:
    matchLabels:
      app: harbor
      component: nginx
      release: harbor
