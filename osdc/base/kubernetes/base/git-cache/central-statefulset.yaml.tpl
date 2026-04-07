apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: git-cache-central
  namespace: kube-system
  labels:
    app: git-cache-central
    app.kubernetes.io/name: git-cache-central
    app.kubernetes.io/component: git-cache
spec:
  serviceName: git-cache-central-headless
  replicas: __REPLICAS__
  updateStrategy:
    type: RollingUpdate
    rollingUpdate:
      partition: 0
  selector:
    matchLabels:
      app: git-cache-central
  template:
    metadata:
      labels:
        app: git-cache-central
    spec:
      priorityClassName: system-cluster-critical
      tolerations:
        - key: CriticalAddonsOnly
          operator: Exists
          effect: NoSchedule
      affinity:
        podAntiAffinity:
          preferredDuringSchedulingIgnoredDuringExecution:
            - weight: 100
              podAffinityTerm:
                labelSelector:
                  matchLabels:
                    app: git-cache-central
                topologyKey: kubernetes.io/hostname
      containers:
        - name: central
          image: public.ecr.aws/docker/library/python:3.12-alpine
          command: ["python3", "/scripts/central.py"]
          ports:
            - name: rsync
              containerPort: 873
              protocol: TCP
            - name: metrics
              containerPort: 9101
              protocol: TCP
          env:
            - name: FETCH_INTERVAL
              value: "300"
          resources:
            requests:
              cpu: __CPU_REQUEST__
              memory: __MEMORY_REQUEST__
            limits:
              cpu: __CPU_LIMIT__
              memory: __MEMORY_LIMIT__
          readinessProbe:
            tcpSocket:
              port: 873
            initialDelaySeconds: 30
            periodSeconds: 10
          livenessProbe:
            tcpSocket:
              port: 873
            initialDelaySeconds: 60
            periodSeconds: 30
          volumeMounts:
            - name: data
              mountPath: /data
            - name: config
              mountPath: /config/rsyncd.conf
              subPath: rsyncd.conf
              readOnly: true
            - name: scripts
              mountPath: /scripts/central.py
              subPath: central.py
              readOnly: true
      volumes:
        - name: config
          configMap:
            name: git-cache-central-config
        - name: scripts
          configMap:
            name: git-cache-central-config
            defaultMode: 0755
  volumeClaimTemplates:
    - metadata:
        name: data
      spec:
        accessModes: ["ReadWriteOnce"]
        storageClassName: gp3
        resources:
          requests:
            storage: __STORAGE_SIZE__
