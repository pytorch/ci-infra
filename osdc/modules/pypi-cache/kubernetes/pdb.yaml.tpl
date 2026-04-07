# pypi-cache PodDisruptionBudget template (per-CUDA version).
# One PDB is generated per CUDA slug by scripts/python/generate_manifests.py.
# Ensures at least one pod per slug remains available during voluntary
# disruptions (node drains, rolling updates, cluster upgrades).
# Placeholders: __NAMESPACE__, __CUDA_SLUG__
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: pypi-cache-__CUDA_SLUG__
  namespace: __NAMESPACE__
  labels:
    app: pypi-cache
    cuda-version: __CUDA_SLUG__
    app.kubernetes.io/name: pypi-cache
    app.kubernetes.io/component: package-cache
spec:
  minAvailable: 1
  selector:
    matchLabels:
      app: pypi-cache
      cuda-version: __CUDA_SLUG__
