# On-demand PyTorch build worker (Phase 1b — first repro of an uncached commit fast).
#
# An always-on worker on the build node that drains a queue on the shared EFS:
# requesters (gpu-dev repro / stage-pytorch) drop a `<sha>.req` marker into
# /ccache_shared/prebuilt/build-queue/ and poll for the by-sha artifact to appear.
# The worker builds the requested SHA incrementally (its own persistent tree, warm
# ccache, mold) and publishes by-sha/<sha> — exactly the artifact the consume side
# already knows how to stage with zero build.
#
# Coordination is entirely via the shared EFS both sides already mount — no new
# networking, RBAC, or lambda changes. The worker writes a `.worker-alive`
# heartbeat each loop; requesters only wait when it's fresh, so if the worker isn't
# deployed they fall straight through to the in-pod build (zero regression).
#
# Builds at /home/dev/pytorch (same absolute path as the cron + dev pods) so the
# build/ CMake cache paths are pod-compatible — a staged tree stays incrementally
# rebuildable in the pod. Own hostPath (/mnt/ondemand-build) so it never collides
# with the hourly cron's tree; shared ccache is concurrency-safe.

resource "kubernetes_deployment_v1" "pytorch_ondemand" {
  depends_on = [kubernetes_namespace.management, null_resource.docker_build_and_push]
  metadata {
    name      = "pytorch-ondemand-builder"
    namespace = "management"
    labels    = { app = "pytorch-ondemand-builder" }
  }

  spec {
    replicas = 1
    strategy { type = "Recreate" } # single persistent tree; never two at once
    selector { match_labels = { app = "pytorch-ondemand-builder" } }

    template {
      metadata { labels = { app = "pytorch-ondemand-builder" } }
      spec {
        node_selector  = { NodeType = "build" }
        restart_policy = "Always"

        container {
          name = "builder"
          # Hash-tagged (not :latest): a rebuilt image = a new tag, so this
          # Deployment's template changes and k8s rolls it automatically, and
          # IfNotPresent pulls the new tag (it's "not present") — no Always, no
          # manual recycle. The build node has no image-prepuller, so :latest would
          # stay stale here.
          image             = local.full_image_uri
          image_pull_policy = "IfNotPresent"
          command           = ["/bin/bash", "-lc"]
          args = [<<-EOT
            set -uo pipefail
            SRC=/home/dev/pytorch
            PREBUILT=/ccache_shared/prebuilt
            QUEUE="$PREBUILT/build-queue"
            BYSHA="$PREBUILT/by-sha"
            GH=https://github.com/pytorch/pytorch.git
            mkdir -p "$QUEUE" "$BYSHA"
            # world-writable (sticky, like /tmp): dev-user pods enqueue <sha>.req here
            # and publish their own builds to by-sha; the worker runs as root.
            chmod 1777 "$QUEUE" "$BYSHA" 2>/dev/null || true

            # --- toolchain (matches the cron, so ccache entries are identical) ---
            export PATH=/usr/local/cuda-13.2/bin:$PATH
            export CUDA_HOME=/usr/local/cuda-13.2
            export CUDACXX=/usr/local/cuda-13.2/bin/nvcc
            export CMAKE_CUDA_COMPILER=/usr/local/cuda-13.2/bin/nvcc
            export USE_CUDA=1
            export TORCH_CUDA_ARCH_LIST="9.0;10.0"
            export BUILD_TEST=0
            export MAX_JOBS=96
            export CCACHE_DIR=/ccache_shared
            export CCACHE_MAXSIZE=250G
            export CMAKE_C_COMPILER_LAUNCHER=ccache
            export CMAKE_CXX_COMPILER_LAUNCHER=ccache
            export CMAKE_CUDA_COMPILER_LAUNCHER=ccache
            MOLD=""; command -v mold >/dev/null 2>&1 && MOLD="mold -run"
            mkdir -p "$CCACHE_DIR"

            if [ ! -d "$SRC/.git" ]; then
              echo "[ondemand] fresh clone into $SRC"
              git clone "$GH" "$SRC" || true
            fi
            git config --global --add safe.directory "$SRC" 2>/dev/null || true

            build_sha() {
              local sha="$1"
              cd "$SRC" || return 1
              git fetch --force origin "$sha" 2>/dev/null || git fetch --force origin 2>/dev/null || true
              git checkout -f "$sha" 2>/dev/null || { echo "[ondemand] checkout $sha failed"; return 1; }
              git -c protocol.file.allow=always submodule update --init --recursive --jobs 8 2>/dev/null || true
              pip install --break-system-packages -r requirements.txt 2>&1 | tail -1 || true
              $MOLD python -m pip install --break-system-packages -e . --no-build-isolation || { echo "[ondemand] build $sha FAILED"; return 1; }
              local zb; zb=$(command -v zstd 2>/dev/null || true)
              if [ -n "$zb" ]; then
                tar -C /home/dev -cf - pytorch | "$zb" -3 -T0 -q -o "$BYSHA/$sha.tar.zst.tmp" && mv "$BYSHA/$sha.tar.zst.tmp" "$BYSHA/$sha.tar.zst" || return 1
              else
                tar -C /home/dev -cf - pytorch | gzip -1 > "$BYSHA/$sha.tar.gz.tmp" && mv "$BYSHA/$sha.tar.gz.tmp" "$BYSHA/$sha.tar.gz" || return 1
              fi
              echo "$sha" > "$BYSHA/$sha.sha"
              return 0
            }

            echo "[ondemand] worker up; draining $QUEUE"
            while true; do
              touch "$QUEUE/.worker-alive" 2>/dev/null || true
              REQ=$(ls -1tr "$QUEUE"/*.req 2>/dev/null | head -1)
              if [ -n "$REQ" ]; then
                SHA=$(basename "$REQ" .req)
                if [ -f "$BYSHA/$SHA.sha" ]; then rm -f "$REQ"; continue; fi
                echo "[ondemand] building $SHA"
                T0=$(date +%s)
                if build_sha "$SHA"; then echo "[ondemand] published $SHA in $(( $(date +%s) - T0 ))s"; else echo "[ondemand] $SHA build failed"; fi
                rm -f "$REQ"
              else
                sleep 5
              fi
            done
          EOT
          ]

          resources {
            requests = {
              cpu    = "16"
              memory = "32Gi"
            }
          }

          volume_mount {
            name       = "ondemand-build"
            mount_path = "/home/dev/pytorch"
          }
          volume_mount {
            name       = "ccache-shared"
            mount_path = "/ccache_shared"
          }
        }

        volume {
          name = "ondemand-build"
          host_path {
            path = "/mnt/ondemand-build"
            type = "DirectoryOrCreate"
          }
        }
        volume {
          name = "ccache-shared"
          nfs {
            server    = local.ccache_efs_dns
            path      = "/"
            read_only = false
          }
        }
      }
    }
  }
}
