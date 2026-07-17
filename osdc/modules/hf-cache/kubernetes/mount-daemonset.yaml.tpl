# Per-node rclone FUSE mount of the cluster's S3 cache (read-only) at host
# /mnt/hf_cache. Job pods hostPath-mount it (HostToContainer); see BEGIN_HF_CACHE
# in modules/arc-runners/templates/runner.yaml.tpl. Reads are lazy and cached on
# NVMe (LRU). Writes go via the GitHub-OIDC refresh path, not this mount.
#
# deploy.sh renders this once per GPU-count tier (via __GPU_OP__/__MULTI_GPU_COUNTS__),
# each with a memory limit scaled to that tier. The instance-gpu-count affinity keeps
# the tiers mutually exclusive (exactly one mount per node).
#
# Placeholders (deploy.sh): __NAMESPACE__ __BUCKET__ __REGION__ __RCLONE_IMAGE__
# __VFS_CACHE_MAX_SIZE__ __TAINT_REMOVER_IMAGE__ __RCLONE_MEMORY_LIMIT__ __GOMEMLIMIT__
# __DS_NAME__ __GPU_OP__ __MULTI_GPU_COUNTS__
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: __DS_NAME__
  namespace: __NAMESPACE__
  labels:
    osdc.io/module: hf-cache
    app.kubernetes.io/name: hf-cache
    app.kubernetes.io/component: mount
spec:
  selector:
    matchLabels:
      app: __DS_NAME__

  updateStrategy:
    type: RollingUpdate
    rollingUpdate:
      maxUnavailable: "25%"

  template:
    metadata:
      labels:
        app: __DS_NAME__
        osdc.io/module: hf-cache
    spec:
      serviceAccountName: hf-cache-mount
      priorityClassName: system-node-critical

      # prepare-host-mount nsenters the host's PID 1 to mount in the host NS.
      hostPID: true

      nodeSelector:
        workload-type: github-runner

      # Per-tier selector (deploy.sh renders one DaemonSet per gpu-count). instance-gpu-count
      # is absent on non-GPU nodes, so the NotIn "rest" tier also covers CPU.
      affinity:
        nodeAffinity:
          requiredDuringSchedulingIgnoredDuringExecution:
            nodeSelectorTerms:
              - matchExpressions:
                  - key: karpenter.k8s.aws/instance-gpu-count
                    operator: __GPU_OP__
                    values: __MULTI_GPU_COUNTS__

      # Schedule before the node-init.osdc.io/* taints clear, so the mount precedes
      # runner pods. operator:Exists avoids a deadlock from missing one (cf. cache-enforcer).
      tolerations:
        - operator: Exists

      # Make the host /mnt/hf_cache an rshared mount point before rclone binds it: a
      # plain hostPath dir gives Bidirectional no host peer, so the FUSE never reaches
      # job pods (they see an empty dir). nsenter-into-host: cf. cache-enforcer.
      initContainers:
        - name: prepare-host-mount
          image: __TAINT_REMOVER_IMAGE__
          securityContext:
            privileged: true
          command:
            - /bin/sh
            - -c
            - |
              set -eu
              exec /proc/1/root/usr/bin/nsenter -t 1 -m -- /bin/sh -c '
                mkdir -p /mnt/hf_cache /mnt/hf-cache-vfs
                grep -q " /mnt/hf_cache " /proc/mounts || mount --bind /mnt/hf_cache /mnt/hf_cache
                mount --make-rshared /mnt/hf_cache
              '

      containers:
        - name: rclone
          image: __RCLONE_IMAGE__
          # FUSE + Bidirectional propagation need privileged.
          securityContext:
            privileged: true
          env:
            # rclone (Go) OOMs from lazy GC, not a real memory need: cap the heap below the
            # cgroup limit so the GC runs before the kernel OOM-kills the mount node-wide.
            # deploy.sh derives this from the tier's memory (~90%, in Go's MiB/GiB format —
            # not k8s Mi/Gi), so it tracks __RCLONE_MEMORY_LIMIT__ without runtime arithmetic.
            - name: GOMEMLIMIT
              value: "__GOMEMLIMIT__"
          command:
            - /bin/sh
            - -c
            - |
              set -eu
              MOUNT=/mnt/hf_cache
              CACHE=/mnt/hf-cache-vfs
              # Clear a stale FUSE left by a crash (no preStop) so rclone can remount.
              # fusermount only — `umount` would drop the host bind mount.
              fusermount -uz "$MOUNT" 2>/dev/null || true
              mkdir -p "$MOUNT" "$CACHE"

              # "<N>%" scales the cap to N% of the cache disk; absolute (200G) as-is.
              # df -k + NF-4 is busybox-safe and survives a wrapped device-name line.
              VFS_MAX="__VFS_CACHE_MAX_SIZE__"
              case "$VFS_MAX" in
                *%)
                  PCT=$(printf '%s' "$VFS_MAX" | tr -dc '0-9')
                  TOTAL_GB=$(( $(df -k "$CACHE" | tail -1 | awk '{print $(NF-4)}') / 1048576 ))
                  VFS_MAX="$(( TOTAL_GB * PCT / 100 ))G"
                  ;;
              esac
              echo "VFS cache cap: $VFS_MAX"

              # Background rclone so we can drop a sentinel once mounted — the
              # taint-remover waits on that rather than inspecting the host, which
              # keeps it unprivileged. Creds via IRSA (env_auth).
              #
              # RSS control (keeps rclone under the per-tier reserve; large
              # safetensors reads OOM-killed the mount on the small 1-GPU tier):
              #   --buffer-size 4M  shrinks the per-open-file in-RAM read-ahead
              #     4x from rclone's 16Mi default — the dominant RSS driver when a
              #     model opens many shards at once. Kept non-zero so cold reads
              #     retain some prefetch (vfs-cache-mode full serves the rest from
              #     the on-disk cache).
              #   --use-mmap        returns freed buffers to the OS instead of Go
              #     retaining them as process RSS.
              rclone mount \
                ":s3,provider=AWS,env_auth=true,region=__REGION__:__BUCKET__" \
                "$MOUNT" \
                --read-only \
                --allow-non-empty \
                --allow-other \
                --dir-cache-time 1h \
                --poll-interval 0 \
                --vfs-cache-mode full \
                --vfs-cache-max-size "$VFS_MAX" \
                --vfs-cache-max-age 24h \
                --vfs-read-chunk-size 128M \
                --buffer-size 4M \
                --use-mmap \
                --cache-dir "$CACHE" \
                --no-modtime \
                --umask 022 \
                --log-level INFO &
              RCLONE_PID=$!
              trap 'kill -TERM "$RCLONE_PID" 2>/dev/null || true' TERM INT
              # Signal on the mount itself (not hub/ contents) so it fires even before
              # the bucket is seeded.
              until grep -q " $MOUNT fuse" /proc/mounts 2>/dev/null; do
                kill -0 "$RCLONE_PID" 2>/dev/null || break
                sleep 1
              done
              touch /run/hf-cache-signal/mounted 2>/dev/null || true
              wait "$RCLONE_PID"
          lifecycle:
            preStop:
              exec:
                command:
                  - /bin/sh
                  - -c
                  - "fusermount -uz /mnt/hf_cache || true"
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
          # An rclone OOM drops the mount node-wide. Memory is tiered by GPU count and
          # reserved (request == limit) — see deploy.sh MOUNT_TIERS. GOMEMLIMIT (env above)
          # is derived from this limit so the Go GC caps the heap first.
          resources:
            requests:
              cpu: 100m
              memory: __RCLONE_MEMORY_LIMIT__
            limits:
              cpu: "1"
              memory: __RCLONE_MEMORY_LIMIT__
          volumeMounts:
            - name: hf-cache
              mountPath: /mnt/hf_cache
              mountPropagation: Bidirectional
            - name: hf-cache-vfs
              mountPath: /mnt/hf-cache-vfs
            - name: mount-signal
              mountPath: /run/hf-cache-signal

        # Clears the node-init.osdc.io/hf-cache gate once rclone signals readiness.
        # Waiting on the sentinel (not the host mount table) keeps it unprivileged.
        - name: taint-remover
          image: __TAINT_REMOVER_IMAGE__
          command:
            - /bin/sh
            - -c
            - |
              set -eu
              while [ ! -e /run/hf-cache-signal/mounted ]; do sleep 2; done
              echo "mount signalled — removing startup taint."
              python3 /opt/taint-remover/taint_remover.py node-init.osdc.io/hf-cache
              echo "taint removed; idling."
              exec sleep infinity
          env:
            - name: NODE_NAME
              valueFrom:
                fieldRef:
                  fieldPath: spec.nodeName
          securityContext:
            allowPrivilegeEscalation: false
            capabilities:
              drop:
                - ALL
          resources:
            requests:
              cpu: 10m
              memory: 32Mi
            limits:
              cpu: 50m
              memory: 64Mi
          volumeMounts:
            - name: taint-remover-lib
              mountPath: /opt/taint-remover
              readOnly: true
            - name: mount-signal
              mountPath: /run/hf-cache-signal
              readOnly: true

      volumes:
        - name: hf-cache
          hostPath:
            path: /mnt/hf_cache
            type: DirectoryOrCreate
        - name: hf-cache-vfs
          hostPath:
            path: /mnt/hf-cache-vfs
            type: DirectoryOrCreate
        # taint_remover.py — deploy.sh renders this ConfigMap into the namespace.
        - name: taint-remover-lib
          configMap:
            name: node-taint-remover-lib
        # Sentinel: rclone touches it when mounted; taint-remover waits on it.
        - name: mount-signal
          emptyDir: {}
