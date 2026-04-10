# pypi-cache Deployment template (per-CUDA version).
# One Deployment is generated per CUDA slug by scripts/python/generate_manifests.py.
# Populated from clusters.yaml config.
# Placeholders: NAMESPACE, CUDA_SLUG, REPLICAS, IMAGE, NGINX_IMAGE,
#   INTERNAL_PORT, WORKERS, NGINX_CPU, NGINX_MEMORY, NGINX_CACHE_SIZE,
#   SERVER_CPU, SERVER_MEMORY, NODE_SELECTOR_BLOCK,
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
          command: ["mkdir", "-p", "/data/wheelhouse/__CUDA_SLUG__", "/data/logs/__CUDA_SLUG__", "/data/logs/upstream", "/tmp/nginx-root"]
          volumeMounts:
            - name: data
              mountPath: /data
            - name: nginx-tmp
              mountPath: /tmp
__INIT_NVME_BLOCK__

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
              path: /nginx-health
              port: http
            initialDelaySeconds: 5
            periodSeconds: 10
            timeoutSeconds: 3
            failureThreshold: 5

          livenessProbe:
            httpGet:
              path: /health
              port: http
            initialDelaySeconds: 10
            periodSeconds: 30
            timeoutSeconds: 10
            failureThreshold: 5

          volumeMounts:
            - name: nginx-config
              mountPath: /etc/nginx/nginx.conf
              subPath: nginx.conf
              readOnly: true
            - name: nginx-config
              mountPath: /etc/nginx/merge_indexes.js
              subPath: merge_indexes.js
              readOnly: true
            - name: nginx-cache
              mountPath: /var/cache/nginx
            - name: nginx-tmp
              mountPath: /tmp
            - name: data
              mountPath: /data/logs/upstream
              subPath: logs/upstream

        - name: pypiserver
          image: __IMAGE__
          command: ["/bin/sh", "-c"]
          args:
            - >-
              pypi-server run
              -p __INTERNAL_PORT__
              --backend cached-dir
              --server gunicorn
              --disable-fallback
              --health-endpoint /health
              -P . -a .
              /data/wheelhouse/__CUDA_SLUG__/

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
            - name: pypiserver-tmp
              mountPath: /tmp

        - name: nginx-exporter
          image: docker.io/nginx/nginx-prometheus-exporter:1.4.1
          args:
            - "--nginx.scrape-uri=http://127.0.0.1:8080/stub_status"
          ports:
            - name: metrics
              containerPort: 9113
              protocol: TCP
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities:
              drop: ["ALL"]
          resources:
            requests:
              cpu: 10m
              memory: 16Mi
            limits:
              cpu: 50m
              memory: 32Mi

      volumes:
        - name: data
          persistentVolumeClaim:
            claimName: pypi-cache-data
        - name: nginx-config
          configMap:
            name: pypi-cache-nginx-config
__NGINX_CACHE_VOLUME__
        - name: nginx-tmp
          emptyDir:
            medium: Memory
            sizeLimit: 64Mi
        - name: pypiserver-tmp
          emptyDir:
            medium: Memory
            sizeLimit: 64Mi
