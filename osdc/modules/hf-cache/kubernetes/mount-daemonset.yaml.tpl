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
# __DS_NAME__ __GPU_OP__ __MULTI_GPU_COUNTS__ __BUFFER_SIZE__
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
              # Runs in the host (PID 1) mount namespace. Every guard below tests the
              # LIVE path with mountpoint(1) — never a raw `grep /proc/mounts` scan. A
              # table scan also matches a mount that has been shadowed by a later bind
              # of a parent dir (an orphaned /mnt/hf_cache hidden under the /mnt bind).
              # Acting on such a mount by path fails ("not a mountpoint"), and under
              # set -e that turned this init into an unrecoverable CrashLoopBackOff while
              # the node's startup taint was already cleared — so jobs kept landing on a
              # node whose cache mount was silently absent (cf. #876 self-heal regression).
              exec /proc/1/root/usr/bin/nsenter -t 1 -m -- /bin/sh -euc '
                HF=/mnt/hf_cache
                mkdir -p "$HF" /mnt/hf-cache-vfs

                # rclone Bidirectional-mounts the parent /mnt, so /mnt must be an rshared
                # mountpoint for the FUSE to reach job pods. Bind only if it is not already
                # a mountpoint, and use rbind so an existing submount under /mnt is carried
                # into the new bind instead of being shadowed — a plain non-recursive bind
                # is what orphaned the old /mnt/hf_cache mount and wedged this init.
                mountpoint -q /mnt || mount --rbind /mnt /mnt
                mount --make-rshared /mnt || true

                # Clear whatever occupies the live /mnt/hf_cache. A crashed rclone leaves a
                # dead FUSE (fails to stat); a prior run of this init can leave an xfs
                # self-bind (stats fine — a stat-gated loop would miss it). Peel a FUSE via
                # fusermount3 (the FUSE3 binary on AL2023 hosts; fusermount is not installed)
                # and anything else via umount -l, until the live path is no longer a
                # mountpoint. Bounded so it can never spin forever.
                i=0
                while mountpoint -q "$HF" && [ "$i" -lt 10 ]; do
                  fusermount3 -uz "$HF" 2>/dev/null || umount -l "$HF" 2>/dev/null || break
                  i=$((i + 1))
                done

                # Re-establish /mnt/hf_cache as its own rshared submount. Guard on the live
                # path so a shadowed orphan in the mount table cannot make us skip the bind
                # (which left make-rshared to fail on a non-mountpoint and crash-loop).
                mountpoint -q "$HF" || mount --bind "$HF" "$HF"
                mount --make-rshared "$HF" || true
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
              # Clear a stale FUSE left by a crash so rclone can remount. fusermount3 is
              # the FUSE3 binary in the rclone image (fusermount is not installed); it
              # clears just the FUSE, leaving the bind mount (a plain umount would drop it).
              fusermount3 -uz "$MOUNT" 2>/dev/null || true
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
              # RSS control (keeps rclone under the per-tier reserve). Per-open-file
              # in-RAM read-ahead is the dominant RSS driver when a model opens many
              # shards at once, and peak concurrent opens scale with node pod-density,
              # so --buffer-size is set per tier by deploy.sh (__BUFFER_SIZE__):
              #   CPU catch-all (640Mi, packed 48xl/metal nodes) → 0: read-ahead off,
              #     served from the vfs-cache-mode full on-disk cache. High pod density
              #     means many concurrent readers on one mount, so any non-zero buffer
              #     OOMs the small reserve.
              #   1-GPU tier (640Mi) → 0: like CPU. rclone RAM ~= buffer-size x concurrent
              #     open files, so even 1M x ~146 shards (~146MB) eats headroom the rclone
              #     heap/metadata needs at 512Mi; read-ahead off, served from the disk cache.
              #   multi-GPU tiers (1-4Gi) → 4M: roomier reserve, so keep more prefetch.
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
                --buffer-size __BUFFER_SIZE__ \
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
                  - "fusermount3 -uz /mnt/hf_cache || true"
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
            # Mount the parent /mnt, not /mnt/hf_cache: containerd stats the volume source
            # at container-create, so a dead FUSE there would block every restart (the init
            # container doesn't re-run on an in-place restart). rclone's fusermount3 -uz
            # above then clears it. The vfs cache lives under /mnt too — no separate volume.
            - name: hf-cache-parent
              mountPath: /mnt
              mountPropagation: Bidirectional
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
        # Parent of /mnt/hf_cache and /mnt/hf-cache-vfs — see rclone volumeMounts.
        - name: hf-cache-parent
          hostPath:
            path: /mnt
            type: Directory
        # taint_remover.py — deploy.sh renders this ConfigMap into the namespace.
        - name: taint-remover-lib
          configMap:
            name: node-taint-remover-lib
        # Sentinel: rclone touches it when mounted; taint-remover waits on it.
        - name: mount-signal
          emptyDir: {}
