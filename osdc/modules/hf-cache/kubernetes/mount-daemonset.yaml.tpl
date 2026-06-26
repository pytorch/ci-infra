# Per-node rclone FUSE mount of the cluster's S3 cache (read-only) at host
# /mnt/hf_cache. Job pods hostPath-mount it (HostToContainer); see BEGIN_HF_CACHE
# in modules/arc-runners/templates/runner.yaml.tpl. Reads are lazy and cached on
# NVMe (LRU). Writes go via the GitHub-OIDC refresh path, not this mount.
#
# Placeholders (deploy.sh): __NAMESPACE__ __BUCKET__ __REGION__ __RCLONE_IMAGE__
# __VFS_CACHE_MAX_SIZE__ __TAINT_REMOVER_IMAGE__
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

      # taint-remover reads the host mount table (/proc/1/mounts) to confirm the
      # FUSE is live on the host before clearing the scheduling gate.
      hostPID: true

      # Runner/workflow nodes are labelled workload-type=github-runner.
      nodeSelector:
        workload-type: github-runner

      # Tolerate every taint so the mount schedules FIRST on a fresh node —
      # ahead of the node-init.osdc.io/* startup taints clearing — and brings up
      # the FUSE before any runner pod. Enumerating taints risks a chicken-and-egg
      # deadlock if one is missed (same rationale as cache-enforcer); a single
      # `operator: Exists` matches all. Isolation is enforced by the nodeSelector.
      tolerations:
        - operator: Exists

      # Make /mnt/hf_cache a shared mount point in the HOST mount namespace before
      # rclone mounts. A plain hostPath dir is not a mount point, so the rclone
      # container's Bidirectional FUSE has no shared host peer to propagate into
      # and job pods (HostToContainer) only ever see the empty dir. Init runs to
      # completion before the rclone container binds the volume, so the bind then
      # joins the shared peer group and the FUSE propagates to the host. Same
      # /proc/1/root nsenter-into-host pattern as cache-enforcer.
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
          command:
            - /bin/sh
            - -c
            - |
              set -eu
              MOUNT=/mnt/hf_cache
              CACHE=/mnt/hf-cache-vfs
              # A crashed/killed container can't run preStop, so a stale FUSE mount
              # is left and rclone then fails with "directory already mounted".
              # Clear only the FUSE (fusermount), never `umount` — that would tear
              # down the host bind mount the initContainer set up for propagation.
              fusermount -uz "$MOUNT" 2>/dev/null || true
              mkdir -p "$MOUNT" "$CACHE"

              # VFS cache cap. A "<N>%" value scales to N% of the cache disk so
              # bigger nodes cache more (a100 ~1TB) and smaller ones less (g5/g6
              # ~600GB); an absolute value (e.g. 200G) is used as-is. df -k + NF-4
              # is busybox-safe and survives a wrapped device-name line.
              VFS_MAX="__VFS_CACHE_MAX_SIZE__"
              case "$VFS_MAX" in
                *%)
                  PCT=$(printf '%s' "$VFS_MAX" | tr -dc '0-9')
                  TOTAL_GB=$(( $(df -k "$CACHE" | tail -1 | awk '{print $(NF-4)}') / 1048576 ))
                  VFS_MAX="$(( TOTAL_GB * PCT / 100 ))G"
                  ;;
              esac
              echo "VFS cache cap: $VFS_MAX"

              # Credentials via IRSA (env_auth). /mnt/hf_cache/hub maps to
              # s3://__BUCKET__/hub. Run rclone in the background so that, once the
              # FUSE is up, we can drop a sentinel file the (unprivileged) taint-
              # remover sidecar waits on — it then needs no hostPID/privileged to
              # detect readiness.
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
                --cache-dir "$CACHE" \
                --no-modtime \
                --umask 022 \
                --log-level INFO &
              RCLONE_PID=$!
              trap 'kill -TERM "$RCLONE_PID" 2>/dev/null || true' TERM INT
              # Signal once the FUSE is actually mounted (content-independent, so it
              # fires even before the bucket is seeded). /proc/mounts here is this
              # container's own mount namespace, where rclone created the mount.
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
            - name: mount-signal
              mountPath: /run/hf-cache-signal

        # Clears the node-init.osdc.io/hf-cache startup taint once rclone signals
        # the FUSE is up (a sentinel file on a shared emptyDir). Using the sentinel
        # instead of reading the host mount table lets this run UNPRIVILEGED — no
        # privileged, no use of hostPID, all capabilities dropped. Unlike rclone it
        # touches nothing on the node; it only patches its own node via the API.
        - name: taint-remover
          image: __TAINT_REMOVER_IMAGE__
          command:
            - /bin/sh
            - -c
            - |
              set -eu
              # Wait for rclone to signal the mount is live, then clear the gate.
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
        # Shared taint_remover.py library (ConfigMap rendered into this namespace
        # by deploy.sh from base/kubernetes/node-taint-remover/lib/).
        - name: taint-remover-lib
          configMap:
            name: node-taint-remover-lib
        # Shared sentinel: rclone touches a file here once the FUSE is up; the
        # taint-remover waits on it (emptyDir sharing — no mount propagation needed,
        # so the sidecar needs no host access).
        - name: mount-signal
          emptyDir: {}
