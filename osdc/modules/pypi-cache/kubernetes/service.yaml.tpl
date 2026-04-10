# pypi-cache Service template (per-CUDA version).
# One ClusterIP Service is generated per CUDA slug by scripts/python/generate_manifests.py.
# Populated from clusters.yaml config.
# Placeholders: __NAMESPACE__, __CUDA_SLUG__, __SERVER_PORT__
apiVersion: v1
kind: Service
metadata:
  name: pypi-cache-__CUDA_SLUG__
  namespace: __NAMESPACE__
  labels:
    app: pypi-cache
    cuda-version: __CUDA_SLUG__
    app.kubernetes.io/name: pypi-cache
    app.kubernetes.io/component: package-cache
spec:
  type: ClusterIP
  selector:
    app: pypi-cache
    cuda-version: __CUDA_SLUG__
  ports:
    - name: http
      port: __SERVER_PORT__
      targetPort: http
      protocol: TCP
    - name: metrics
      port: 9113
      targetPort: metrics
      protocol: TCP
