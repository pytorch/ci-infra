# pypi-cache Deployment template (per-CUDA version).
# One Deployment is generated per CUDA slug by scripts/python/generate_manifests.py.
# Populated from clusters.yaml config.
# Placeholders: NAMESPACE, CUDA_SLUG, REPLICAS, IMAGE, NGINX_IMAGE,
#   INTERNAL_PORT, WORKERS, NGINX_CPU, NGINX_MEMORY, NGINX_CACHE_SIZE,
#   SERVER_CPU, SERVER_MEMORY, LOG_MAX_AGE_DAYS, NODE_SELECTOR_BLOCK,
#   TOLERATIONS_ENTRIES
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

      __NODE_SELECTOR_BLOCK__
      tolerations:
__TOLERATIONS_ENTRIES__

      initContainers:
        - name: init-dirs
          image: busybox:1.36
          command: ["mkdir", "-p", "/data/wheelhouse/__CUDA_SLUG__", "/data/logs/__CUDA_SLUG__"]
          volumeMounts:
            - name: data
              mountPath: /data

      containers:
        - name: nginx
          image: __NGINX_IMAGE__

          ports:
            - name: http
              containerPort: 8080
              protocol: TCP

          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities:
              drop: ["ALL"]

          resources:
            requests:
              cpu: __NGINX_CPU__
              memory: __NGINX_MEMORY__
            limits:
              cpu: __NGINX_CPU__
              memory: __NGINX_MEMORY__

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
            - name: nginx-config
              mountPath: /etc/nginx/nginx.conf
              subPath: nginx.conf
              readOnly: true
            - name: nginx-cache
              mountPath: /var/cache/nginx
            - name: nginx-tmp
              mountPath: /tmp

        - name: pypiserver
          image: __IMAGE__
          command: ["/bin/sh", "-c"]
          args:
            - >-
              pypi-server run
              -p __INTERNAL_PORT__
              --backend simple-dir
              --server gunicorn
              --disable-fallback
              --health-endpoint /health
              -P . -a .
              /data/wheelhouse/__CUDA_SLUG__/
              2>&1 | python3 /scripts/log_rotator.py
              --log-dir /data/logs/__CUDA_SLUG__/
              --max-age-days __LOG_MAX_AGE_DAYS__

          ports:
            - name: pypi-internal
              containerPort: __INTERNAL_PORT__
              protocol: TCP

          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities:
              drop: ["ALL"]

          env:
            - name: PYTHONUNBUFFERED
              value: "1"
            - name: GUNICORN_CMD_ARGS
              value: "--workers __WORKERS__ --timeout 300"
            - name: NODE_NAME
              valueFrom:
                fieldRef:
                  fieldPath: spec.nodeName
            - name: CUDA_SLUG
              value: "__CUDA_SLUG__"

          resources:
            requests:
              cpu: __SERVER_CPU__
              memory: __SERVER_MEMORY__
            limits:
              cpu: __SERVER_CPU__
              memory: __SERVER_MEMORY__

          volumeMounts:
            - name: data
              mountPath: /data
            - name: scripts
              mountPath: /scripts
              readOnly: true
            - name: pypiserver-tmp
              mountPath: /tmp

      volumes:
        - name: data
          persistentVolumeClaim:
            claimName: pypi-cache-data
        - name: scripts
          configMap:
            name: pypi-cache-scripts
            defaultMode: 0755
        - name: nginx-config
          configMap:
            name: pypi-cache-nginx-config
        - name: nginx-cache
          emptyDir:
            sizeLimit: __NGINX_CACHE_SIZE__
        - name: nginx-tmp
          emptyDir:
            medium: Memory
            sizeLimit: 64Mi
        - name: pypiserver-tmp
          emptyDir:
            medium: Memory
            sizeLimit: 64Mi
