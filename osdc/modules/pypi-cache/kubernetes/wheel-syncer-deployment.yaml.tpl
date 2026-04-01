# pypi-cache wheel-syncer Deployment template.
# Syncs built wheel packages from S3 to EFS wheelhouse.
# Placeholders: NAMESPACE, CUDA_SLUGS
apiVersion: apps/v1
kind: Deployment
metadata:
  name: pypi-wheel-syncer
  namespace: __NAMESPACE__
  labels:
    app: pypi-wheel-syncer
    app.kubernetes.io/name: pypi-wheel-syncer
    app.kubernetes.io/component: wheel-syncer
spec:
  replicas: 1
  selector:
    matchLabels:
      app: pypi-wheel-syncer
  template:
    metadata:
      labels:
        app: pypi-wheel-syncer
        app.kubernetes.io/name: pypi-wheel-syncer
        app.kubernetes.io/component: wheel-syncer
    spec:
      serviceAccountName: pypi-wheel-syncer

      securityContext:
        runAsNonRoot: true
        runAsUser: 65534
        runAsGroup: 65534
        fsGroup: 65534

      tolerations:
        - key: CriticalAddonsOnly
          operator: Equal
          value: "true"
          effect: NoSchedule

      initContainers:
        - name: install-deps
          image: python:3.12-alpine
          command: ["pip", "install", "--no-cache-dir", "boto3==1.35.0", "--target=/pip-packages"]

          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: false
            capabilities:
              drop: ["ALL"]

          resources:
            requests:
              cpu: 100m
              memory: 256Mi
            limits:
              cpu: 500m
              memory: 512Mi

          volumeMounts:
            - name: pip-packages
              mountPath: /pip-packages

      containers:
        - name: wheel-syncer
          image: python:3.12-alpine
          command: ["python3", "/scripts/wheel_syncer.py"]
          args:
            - "--wheelhouse-dir"
            - "/data/wheelhouse"
            - "--bucket"
            - "pytorch-pypi-wheel-cache"
            - "--slugs"
            - "__CUDA_SLUGS__"
            - "--interval"
            - "60"

          env:
            - name: PYTHONPATH
              value: "/pip-packages"
            - name: PYTHONUNBUFFERED
              value: "1"

          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities:
              drop: ["ALL"]

          resources:
            requests:
              cpu: 100m
              memory: 256Mi
            limits:
              cpu: 500m
              memory: 512Mi

          livenessProbe:
            exec:
              command:
                - python3
                - "-c"
                - "import os, time; assert time.time() - os.path.getmtime('/tmp/last-success') < 600"
            initialDelaySeconds: 300
            periodSeconds: 60

          volumeMounts:
            - name: data
              mountPath: /data
              readOnly: false
            - name: scripts
              mountPath: /scripts
              readOnly: true
            - name: pip-packages
              mountPath: /pip-packages
              readOnly: true
            - name: tmp
              mountPath: /tmp

      volumes:
        - name: data
          persistentVolumeClaim:
            claimName: pypi-cache-data
        - name: scripts
          configMap:
            name: pypi-wheel-syncer-scripts
            defaultMode: 0755
        - name: pip-packages
          emptyDir: {}
        - name: tmp
          emptyDir:
            medium: Memory
            sizeLimit: 64Mi
