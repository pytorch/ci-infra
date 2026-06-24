# Per-node rclone FUSE mount of the cluster's S3 cache (read-write) at host
# /mnt/hf_cache. Job pods hostPath-mount it (HostToContainer); see BEGIN_HF_CACHE
# in modules/arc-runners/templates/runner.yaml.tpl. Reads are lazy and cached on
# NVMe (LRU, --vfs-cache-max-size); writes (ci-refresh-hf-cache runs) upload to S3.
#
# Placeholders (deploy.sh): __NAMESPACE__ __BUCKET__ __REGION__ __RCLONE_IMAGE__
# __VFS_CACHE_MAX_SIZE__
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: hf-cache-mount
  namespace: __NAMESPACE__
  labels:
    osdc.io/module: hf-cache
    app.kubernetes.io/name: hf-cache
    app.kubernetes.io/component: mount
spec:
  selector:
    matchLabels:
      app: hf-cache-mount

  updateStrategy:
    type: RollingUpdate
    rollingUpdate:
      maxUnavailable: "25%"

  template:
    metadata:
      labels:
        app: hf-cache-mount
        osdc.io/module: hf-cache
    spec:
      serviceAccountName: hf-cache-mount
      priorityClassName: system-node-critical

      # Runner/workflow nodes are labelled workload-type=github-runner.
      nodeSelector:
        workload-type: github-runner

      # Tolerate all runner/workflow node taints so the mount precedes jobs.
      tolerations:
        - key: node-fleet
          operator: Exists
          effect: NoSchedule
        - key: instance-type
          operator: Exists
          effect: NoSchedule
        - key: nvidia.com/gpu
          operator: Exists
          effect: NoSchedule
        - key: deploy.osdc.io/refresh-pending
          operator: Exists
          effect: NoSchedule
        - key: CriticalAddonsOnly
          operator: Exists

      containers:
        - name: rclone
          image: __RCLONE_IMAGE__
          # FUSE + Bidirectional propagation need privileged.
          securityContext:
            privileged: true
          command:
            - /bin/sh
            - -c
            - |
              set -eu
              MOUNT=/mnt/hf_cache
              CACHE=/mnt/hf-cache-vfs
              mkdir -p "$MOUNT" "$CACHE"

              # Credentials via IRSA (env_auth). /mnt/hf_cache/hub maps to
              # s3://__BUCKET__/hub. umask 000 so job pods (any uid) can write.
              exec rclone mount \
                ":s3,provider=AWS,env_auth=true,region=__REGION__:__BUCKET__" \
                "$MOUNT" \
                --allow-other \
                --dir-cache-time 1h \
                --poll-interval 0 \
                --vfs-cache-mode full \
                --vfs-cache-max-size __VFS_CACHE_MAX_SIZE__ \
                --vfs-cache-max-age 24h \
                --vfs-read-chunk-size 128M \
                --cache-dir "$CACHE" \
                --no-modtime \
                --umask 000 \
                --log-level INFO
          lifecycle:
            preStop:
              exec:
                command:
                  - /bin/sh
                  - -c
                  - "fusermount -uz /mnt/hf_cache || umount -l /mnt/hf_cache || true"
          # A hung mount blocks this until timeout → pod restart.
          livenessProbe:
            exec:
              command:
                - /bin/sh
                - -c
                - "ls /mnt/hf_cache >/dev/null"
            initialDelaySeconds: 15
            periodSeconds: 30
            timeoutSeconds: 10
            failureThreshold: 3
          resources:
            requests:
              cpu: 100m
              memory: 256Mi
            limits:
              cpu: "1"
              memory: 1Gi
          volumeMounts:
            - name: hf-cache
              mountPath: /mnt/hf_cache
              mountPropagation: Bidirectional
            - name: hf-cache-vfs
              mountPath: /mnt/hf-cache-vfs

      volumes:
        - name: hf-cache
          hostPath:
            path: /mnt/hf_cache
            type: DirectoryOrCreate
        - name: hf-cache-vfs
          hostPath:
            path: /mnt/hf-cache-vfs
            type: DirectoryOrCreate
