# HF cache mount DaemonSet.
#
# Runs one rclone FUSE mount per node that can host workflow (job) pods,
# exposing the shared S3 model cache read-only at the host path /mnt/hf_cache.
# Job pods hostPath-mount that path (HostToContainer propagation) so the cache
# appears inside the job container — see the BEGIN_HF_CACHE block in
# modules/arc-runners/templates/runner.yaml.tpl.
#
# Reads are lazy: rclone fetches a file from S3 on first open and caches it on
# node-local NVMe (--cache-dir), bounded by --vfs-cache-max-size with LRU
# eviction. A cold Karpenter node therefore only pulls the models its jobs
# actually touch, never the whole bucket.
#
# Each cluster mounts only its own prefix in the shared bucket
# (s3://__BUCKET__/__CLUSTER__) so per-cluster refresh writers never collide.
#
# Placeholders substituted by deploy.sh: __NAMESPACE__ __BUCKET__ __CLUSTER__
# __REGION__ __RCLONE_IMAGE__ __VFS_CACHE_MAX_SIZE__
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

      # Run on every node that can host workflow/job pods. ARC runner and
      # workflow nodes are labelled workload-type=github-runner.
      nodeSelector:
        workload-type: github-runner

      # Tolerate every taint a runner/workflow node may carry, including the
      # graceful-refresh taint, so the mount is present before jobs land.
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
          # FUSE mount requires /dev/fuse and the ability to share the mount
          # back to the host (Bidirectional propagation) — both need privileged.
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

              # IRSA supplies credentials via the AWS SDK env chain (env_auth).
              # Mount this cluster's own prefix; /mnt/hf_cache/hub then maps to
              # s3://__BUCKET__/__CLUSTER__/hub.
              exec rclone mount \
                ":s3,provider=AWS,env_auth=true,region=__REGION__:__BUCKET__/__CLUSTER__" \
                "$MOUNT" \
                --read-only \
                --allow-other \
                --dir-cache-time 1h \
                --poll-interval 0 \
                --vfs-cache-mode full \
                --vfs-cache-max-size __VFS_CACHE_MAX_SIZE__ \
                --vfs-cache-max-age 24h \
                --vfs-read-chunk-size 128M \
                --cache-dir "$CACHE" \
                --no-modtime \
                --umask 022 \
                --log-level INFO
          lifecycle:
            preStop:
              exec:
                command:
                  - /bin/sh
                  - -c
                  - "fusermount -uz /mnt/hf_cache || umount -l /mnt/hf_cache || true"
          # A hung FUSE mount makes this exec block until the probe times out,
          # which restarts the pod and re-establishes the mount.
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
