# pypi-cache PersistentVolumeClaim template.
# Claims EFS-backed storage for shared wheel data across Deployments.
# Populated by scripts/python/generate_manifests.py from clusters.yaml config.
# Placeholders: __NAMESPACE__, __STORAGE_REQUEST__
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: pypi-cache-data
  namespace: __NAMESPACE__
  labels:
    app: pypi-cache
    app.kubernetes.io/name: pypi-cache
    app.kubernetes.io/component: package-cache
spec:
  accessModes:
    - ReadWriteMany
  storageClassName: efs-pypi-cache
  resources:
    requests:
      storage: __STORAGE_REQUEST__
