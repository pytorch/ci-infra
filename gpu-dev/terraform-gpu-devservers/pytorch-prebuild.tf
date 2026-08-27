# Hourly stateful incremental PyTorch build (prebuilt viable/strict).
#
# Runs on the dedicated always-on build node (NodeType=build, m7i.48xlarge,
# 192 vCPU / 768 GB). Produces an importable, incrementally-buildable torch tree
# that dev pods reflink-copy into /home/dev/pytorch on claim — so users "import
# torch" instantly and rebuild their PR diff in seconds, not ~20 min.
#
# Recipe is empirically validated (see CLAUDE.md): CUDA 13.2 (matches the cu13
# nvshmem ABI bundled in the image), TORCH_CUDA_ARCH_LIST="9.0;10.0" (plain —
# cmake auto-adds sm_90a/sm_100a to the cutlass kernels, exactly like trunk CI;
# covers H100 + B200), BUILD_TEST=0 (like CI), ccache on the shared EFS.
#
# Build queue: concurrencyPolicy=Forbid (k8s skips a tick if one is still
# running) + a flock on the shared EFS (belt-and-suspenders against the 3-way
# build collision we hit during bring-up).
#
# Persistence:
#   - The checkout + build/ ninja dir live on the build node's local disk via a
#     hostPath mounted AT /home/dev/pytorch. build/'s CMakeCache bakes absolute
#     paths, so we MUST build at the same path pods use (/home/dev/pytorch) for
#     the in-pod incremental to work. The hostPath survives across hourly runs
#     (node is always-on) → every run is incremental.
#   - ccache lives on the shared ccache EFS (/ccache_shared) — survives node
#     replacement, and is the SAME cache every dev pod uses, so a user's own
#     rebuild benefits from the build node's compiles and vice-versa.
#
# Publish: rsync the built tree to /ccache_shared/prebuilt/pytorch-<arch>/ on the
# shared EFS (only changed objects move each hour). The pytorch-snapshot
# DaemonSet picks it up from there onto each node's NVMe.

locals {
  ccache_efs_dns      = "${aws_efs_file_system.ccache_shared.id}.efs.${local.current_config.aws_region}.amazonaws.com"
  prebuild_arch       = "x86_64" # build node is m7i (x86). aarch64 = future second build node.
  prebuild_build_path = "/home/dev/pytorch"
}

resource "kubernetes_cron_job_v1" "pytorch_prebuild" {
  depends_on = [kubernetes_namespace.management, null_resource.docker_build_and_push]
  metadata {
    name      = "pytorch-prebuild"
    namespace = "management"
    labels    = { app = "pytorch-prebuild" }
  }

  spec {
    schedule                      = "0 * * * *" # hourly
    concurrency_policy            = "Forbid"    # the build queue: never overlap
    starting_deadline_seconds     = 300
    successful_jobs_history_limit = 2
    failed_jobs_history_limit     = 3

    job_template {
      metadata {
        labels = { app = "pytorch-prebuild" }
      }
      spec {
        backoff_limit           = 0    # a failed build waits for the next hourly tick, no thrash
        active_deadline_seconds = 7200 # 2h safety kill for a wedged build

        template {
          metadata {
            labels = { app = "pytorch-prebuild" }
          }
          spec {
            node_selector  = { NodeType = "build" }
            restart_policy = "Never"

            container {
              name = "build"
              # Hash-tagged (not :latest): a rebuilt image = a new tag, so each cron
              # job pulls it (IfNotPresent -> "not present"). The build node has no
              # image-prepuller, so :latest would stay stale here (no mold/zstd).
              image             = local.full_image_uri
              image_pull_policy = "IfNotPresent"
              command           = ["/bin/bash", "-lc"]
              args = [<<-EOT
                set -uo pipefail
                ARCH=$(uname -m)
                SRC=${local.prebuild_build_path}
                PREBUILT=/ccache_shared/prebuilt
                PUB="$PREBUILT/pytorch-$ARCH"
                mkdir -p "$PREBUILT"

                # --- build queue (belt-and-suspenders vs concurrencyPolicy=Forbid) ---
                exec 9>"$PREBUILT/build-$ARCH.lock"
                if ! flock -n 9; then echo "[prebuild] another build holds the lock; exiting"; exit 0; fi

                # --- by-SHA cache retention (Phase 3): prune to the trailing ~72h ---
                # The by-sha cache (viable/strict bumps + repro usage-fill) is the
                # "snapshot ladder": dozens of points across the window, so any commit
                # is a small delta from a neighbour. Cap it by age so storage stays in
                # budget (~500-650GB). Runs every tick (even when v/s is unchanged).
                BYSHA="$PREBUILT/by-sha"
                if [ -d "$BYSHA" ]; then
                  PRUNED=$(find "$BYSHA" -maxdepth 1 -name '*.tar.*' -mtime +3 2>/dev/null | wc -l | tr -d ' ')
                  if [ "$PRUNED" -gt 0 ]; then
                    find "$BYSHA" -maxdepth 1 \( -name '*.tar.*' -o -name '*.sha' \) -mtime +3 -delete 2>/dev/null || true
                    echo "[prebuild] pruned $PRUNED by-sha entrie(s) older than 72h"
                  fi
                fi

                # --- toolchain (validated recipe) ---
                export PATH=/usr/local/cuda-13.2/bin:$PATH
                export CUDA_HOME=/usr/local/cuda-13.2
                export CUDACXX=/usr/local/cuda-13.2/bin/nvcc
                export CMAKE_CUDA_COMPILER=/usr/local/cuda-13.2/bin/nvcc
                export USE_CUDA=1
                export TORCH_CUDA_ARCH_LIST="9.0;10.0"
                export BUILD_TEST=0
                export MAX_JOBS=128
                export CCACHE_DIR=/ccache_shared
                # A single PyTorch build is 10-20GB of objects; the ccache default
                # (~5GB) evicts constantly (prod was at 37k cleanups / ~45% hits).
                # Size it to hold many commits so repros + the next viable/strict
                # bump mostly hit. EFS is elastic; ~$0.30/GB-mo.
                export CCACHE_MAXSIZE=250G
                export CMAKE_C_COMPILER_LAUNCHER=ccache
                export CMAKE_CXX_COMPILER_LAUNCHER=ccache
                export CMAKE_CUDA_COMPILER_LAUNCHER=ccache
                mkdir -p "$CCACHE_DIR"
                ccache -M 250G 2>/dev/null || true   # persist max_size into the shared cache config

                # --- checkout viable/strict (last green trunk commit) ---
                if [ ! -d "$SRC/.git" ]; then
                  echo "[prebuild] fresh clone into $SRC"
                  git clone https://github.com/pytorch/pytorch.git "$SRC" || exit 1
                fi
                cd "$SRC" || exit 1
                git remote set-url origin https://github.com/pytorch/pytorch.git
                git fetch --force origin viable/strict || { echo "[prebuild] fetch failed"; exit 1; }
                TARGET=$(git rev-parse FETCH_HEAD)
                OLD=$(cat "$PUB.sha" 2>/dev/null || echo none)
                if [ "$TARGET" = "$OLD" ]; then
                  echo "[prebuild] viable/strict unchanged at $TARGET — nothing to do"
                  exit 0
                fi
                echo "[prebuild] building $OLD -> $TARGET"
                git checkout -f "$TARGET" || exit 1
                git submodule update --init --recursive || exit 1

                # --- build (incremental; build/ persists on hostPath) ---
                pip install --break-system-packages -r requirements.txt 2>&1 | tail -2 || true
                # mold links libtorch_cuda.so in ~15s vs ~1-3min for ld -> the relink
                # floor that otherwise dominates an incremental build. mold -run wraps
                # the whole build so every link goes through it. Guarded: no-op until
                # the image ships mold.
                MOLD=""; command -v mold >/dev/null 2>&1 && { MOLD="mold -run"; echo "[prebuild] using mold linker"; }
                T0=$(date +%s)
                $MOLD python -m pip install --break-system-packages -e . --no-build-isolation || { echo "[prebuild] BUILD FAILED"; exit 1; }
                echo "[prebuild] build took $(( $(date +%s) - T0 ))s"

                # --- verify importable + correct archs ---
                python -c "import torch; print('[prebuild] torch', torch.__version__, torch.cuda.get_arch_list())" || { echo "[prebuild] import verify FAILED"; exit 1; }

                # --- publish: single tarball to shared EFS ---
                # rsync of the raw tree (.git + build/ = 100k+ small files) over EFS/NFS
                # dies on per-file round-trips (0 files in 13min observed). tar|zstd reads
                # the small files from LOCAL disk and writes ONE sequential stream to EFS.
                # zstd -T0 uses all cores; .sha is written last as the completion marker.
                # Prefer zstd (faster + smaller, esp. decompress on every node) when
                # present in the image; fall back to gzip (/usr/bin/gzip is base).
                # We rm the other format so exactly one artifact exists and the
                # DaemonSet picks it unambiguously. .sha is written last (the gate).
                ZBIN=$(command -v zstd 2>/dev/null || { [ -x /usr/local/bin/zstd ] && echo /usr/local/bin/zstd; } || true)
                if [ -n "$ZBIN" ]; then
                  echo "[prebuild] publishing tarball (zstd) -> $PUB.tar.zst"
                  rm -f "$PUB.tar.gz"
                  tar -C /home/dev -cf - pytorch | "$ZBIN" -3 -T0 -q -o "$PUB.tar.zst.tmp" || { echo "[prebuild] publish FAILED"; exit 1; }
                  mv "$PUB.tar.zst.tmp" "$PUB.tar.zst"; PUBFILE="$PUB.tar.zst"
                else
                  echo "[prebuild] publishing tarball (gzip) -> $PUB.tar.gz"
                  rm -f "$PUB.tar.zst"
                  tar -C /home/dev -cf - pytorch | /usr/bin/gzip -1 > "$PUB.tar.gz.tmp" || { echo "[prebuild] publish FAILED"; exit 1; }
                  mv "$PUB.tar.gz.tmp" "$PUB.tar.gz"; PUBFILE="$PUB.tar.gz"
                fi
                echo "$TARGET" > "$PUB.sha"
                echo "[prebuild] published $(du -sh "$PUBFILE" | cut -f1) @ $TARGET"

                # --- also seed the by-SHA artifact cache (Phase 1) ---
                # Same bytes, hardlinked (no extra EFS space) so a repro that resolves
                # to exactly this SHA stages a fully-built tree with ZERO build. The
                # .sha marker is written LAST = the completion gate stage-pytorch polls.
                BYSHA="$PREBUILT/by-sha"
                mkdir -p "$BYSHA"
                chmod 1777 "$BYSHA" 2>/dev/null || true   # dev-user pods publish here too
                EXT="$${PUBFILE##*.}"   # zst or gz, whichever we published
                BYSHA_FILE="$BYSHA/$TARGET.tar.$EXT"
                if [ ! -f "$BYSHA_FILE" ]; then
                  ln -f "$PUBFILE" "$BYSHA_FILE" 2>/dev/null || cp "$PUBFILE" "$BYSHA_FILE"
                  echo "$TARGET" > "$BYSHA/$TARGET.sha"
                  echo "[prebuild] seeded by-sha cache: $TARGET.tar.$EXT"
                fi
                ccache -s 2>/dev/null | grep -iE 'hits|misses' | head
                echo "[prebuild] DONE $TARGET"
              EOT
              ]

              resources {
                requests = {
                  cpu    = "32"
                  memory = "64Gi"
                }
                # no limits: the build node is dedicated; let the build use the box
              }

              volume_mount {
                name       = "pytorch-build"
                mount_path = local.prebuild_build_path
              }
              volume_mount {
                name       = "ccache-shared"
                mount_path = "/ccache_shared"
              }
            }

            # Persistent checkout + build/ on the build node's local disk.
            volume {
              name = "pytorch-build"
              host_path {
                path = "/mnt/pytorch-build"
                type = "DirectoryOrCreate"
              }
            }
            # Shared ccache EFS (same cache all dev pods use) — also holds /prebuilt.
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
  }
}
