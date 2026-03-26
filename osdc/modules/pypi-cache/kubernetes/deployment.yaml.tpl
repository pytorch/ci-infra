# pypi-cache Deployment template (per-CUDA version).
# One Deployment is generated per CUDA slug by scripts/python/generate_manifests.py.
# Populated from clusters.yaml config.
# Placeholders: __NAMESPACE__, __CUDA_SLUG__, __REPLICAS__, __IMAGE__,
#   __SERVER_PORT__, __CPU_REQUEST__, __CPU_LIMIT__, __MEMORY_REQUEST__,
#   __MEMORY_LIMIT__, __LOG_MAX_AGE_DAYS__
apiVersion: apps/v1
kind: Deployment
metadata:
  name: pypi-cache-__CUDA_SLUG__
  namespace: __NAMESPACE__
  labels:
    app: pypi-cache
    cuda-version: __CUDA_SLUG__
    app.kubernetes.io/name: pypi-cache
    app.kubernetes.io/component: package-cache
spec:
  replicas: __REPLICAS__
  selector:
    matchLabels:
      app: pypi-cache
      cuda-version: __CUDA_SLUG__
  template:
    metadata:
      labels:
        app: pypi-cache
        cuda-version: __CUDA_SLUG__
        app.kubernetes.io/name: pypi-cache
        app.kubernetes.io/component: package-cache
    spec:
      serviceAccountName: pypi-cache

      securityContext:
        runAsNonRoot: true
        runAsUser: 65534
        runAsGroup: 65534
        fsGroup: 65534

      # Spread replicas across nodes for availability
      affinity:
        podAntiAffinity:
          preferredDuringSchedulingIgnoredDuringExecution:
            - weight: 100
              podAffinityTerm:
                labelSelector:
                  matchLabels:
                    cuda-version: __CUDA_SLUG__
                topologyKey: kubernetes.io/hostname

      tolerations:
        - key: CriticalAddonsOnly
          operator: Exists
          effect: NoSchedule

      initContainers:
        - name: init-dirs
          image: busybox:1.36
          command: ["mkdir", "-p", "/data/wheelhouse/__CUDA_SLUG__", "/data/logs/__CUDA_SLUG__"]
          volumeMounts:
            - name: data
              mountPath: /data

      containers:
        - name: server
          image: __IMAGE__
          command: ["/bin/sh", "-c"]
          args:
            - >-
              pypi-server run
              -p __SERVER_PORT__
              --backend simple-dir
              --server gunicorn
              --fallback-url https://pypi.org/simple/
              --health-endpoint /health
              -P . -a .
              /data/wheelhouse/__CUDA_SLUG__/
              2>&1 | python3 /scripts/log_rotator.py
              --log-dir /data/logs/__CUDA_SLUG__/
              --max-age-days __LOG_MAX_AGE_DAYS__

          ports:
            - name: http
              containerPort: __SERVER_PORT__
              protocol: TCP

          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities:
              drop: ["ALL"]

          env:
            - name: PYTHONUNBUFFERED
              value: "1"
            - name: SERVER_PORT
              value: "__SERVER_PORT__"
            - name: NODE_NAME
              valueFrom:
                fieldRef:
                  fieldPath: spec.nodeName
            - name: CUDA_SLUG
              value: "__CUDA_SLUG__"

          resources:
            requests:
              cpu: __CPU_REQUEST__
              memory: __MEMORY_REQUEST__
            limits:
              cpu: __CPU_LIMIT__
              memory: __MEMORY_LIMIT__

          readinessProbe:
            httpGet:
              path: /health
              port: http
            initialDelaySeconds: 5
            periodSeconds: 10

          livenessProbe:
            httpGet:
              path: /health
              port: http
            initialDelaySeconds: 10
            periodSeconds: 30

          volumeMounts:
            - name: data
              mountPath: /data
            - name: scripts
              mountPath: /scripts
              readOnly: true
            - name: tmp
              mountPath: /tmp

      volumes:
        - name: data
          persistentVolumeClaim:
            claimName: pypi-cache-data
        - name: scripts
          configMap:
            name: pypi-cache-scripts
            defaultMode: 0755
        - name: tmp
          emptyDir:
            medium: Memory
            sizeLimit: 64Mi
