# pypi-cache wants-collector Deployment template.
# Scans pypiserver access logs, filters against PyPI, uploads wants list to S3.
# Placeholders: NAMESPACE, CLUSTER_ID, LOG_MAX_AGE_DAYS, TARGET_PYTHON_VERSIONS,
#   TARGET_ARCHITECTURES, TARGET_MANYLINUX
apiVersion: apps/v1
kind: Deployment
metadata:
  name: pypi-wants-collector
  namespace: __NAMESPACE__
  labels:
    app: pypi-wants-collector
    app.kubernetes.io/name: pypi-wants-collector
    app.kubernetes.io/component: wants-collector
spec:
  replicas: 1
  selector:
    matchLabels:
      app: pypi-wants-collector
  template:
    metadata:
      labels:
        app: pypi-wants-collector
        app.kubernetes.io/name: pypi-wants-collector
        app.kubernetes.io/component: wants-collector
    spec:
      serviceAccountName: pypi-wants-collector

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
        - name: wants-collector
          image: python:3.12-alpine
          command: ["python3", "/scripts/wants_collector.py"]
          args:
            - "--log-dir"
            - "/data/logs/upstream"
            - "--cluster-id"
            - "__CLUSTER_ID__"
            - "--bucket"
            - "pytorch-pypi-wheel-cache"
            - "--interval"
            - "120"
            - "--target-python"
            - "__TARGET_PYTHON_VERSIONS__"
            - "--target-arch"
            - "__TARGET_ARCHITECTURES__"
            - "--target-manylinux"
            - "__TARGET_MANYLINUX__"
            - "--max-log-age-days"
            - "__LOG_MAX_AGE_DAYS__"

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
              cpu: 50m
              memory: 128Mi
            limits:
              cpu: 200m
              memory: 256Mi

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
            name: pypi-wants-collector-scripts
            defaultMode: 0755
        - name: pip-packages
          emptyDir: {}
        - name: tmp
          emptyDir:
            medium: Memory
            sizeLimit: 64Mi
