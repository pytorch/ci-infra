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

              # Credentials via IRSA (env_auth). /mnt/hf_cache/hub maps to
              # s3://__BUCKET__/hub.
              exec rclone mount \
                ":s3,provider=AWS,env_auth=true,region=__REGION__:__BUCKET__" \
                "$MOUNT" \
                --read-only \
                --allow-non-empty \
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

        # Clears the node-init.osdc.io/hf-cache startup taint once the FUSE is
        # live on the host, gating runner pods until the cache is mountable.
        # A runner pod that starts before the FUSE binds the empty host dir
        # (HostToContainer won't backfill a running pod), so the gate is the
        # only reliable way to guarantee jobs see the cache.
        - name: taint-remover
          image: __TAINT_REMOVER_IMAGE__
          command:
            - /bin/sh
            - -c
            - |
              set -eu
              # Wait until rclone's FUSE has propagated to the host mount table.
              # /proc/1/mounts is the host's (hostPID); the rclone container's
              # Bidirectional mount surfaces here once it is up.
              until grep -q ' /mnt/hf_cache fuse.rclone ' /proc/1/mounts 2>/dev/null; do
                echo "waiting for /mnt/hf_cache FUSE mount on host..."
                sleep 2
              done
              echo "FUSE mount present on host — removing startup taint."
              python3 /opt/taint-remover/taint_remover.py node-init.osdc.io/hf-cache
              echo "taint removed; idling."
              exec sleep infinity
          env:
            - name: NODE_NAME
              valueFrom:
                fieldRef:
                  fieldPath: spec.nodeName
          securityContext:
            privileged: true
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
