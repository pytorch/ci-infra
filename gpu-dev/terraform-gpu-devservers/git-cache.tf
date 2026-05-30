# Git Cache Service - In-cluster object cache for fast git clone operations
# Maintains bare copies of pytorch/pytorch AND all its submodules, refreshed every 15 min.
# User pods get a transparent git wrapper that clones from cache,
# then sets origin to GitHub — all subsequent git ops go to GitHub directly.
#
# Also publishes pytorch-worktree-master.tar.gz: a ready-to-use working tree
# (master + submodules already checked out, .git kept, origins pointed back at
# GitHub) so pods can drop pytorch into /home/dev with a single extract instead
# of a cold checkout + per-submodule clone on the critical path.

# Management namespace for infrastructure services (git-cache, monitoring, etc.)
resource "kubernetes_namespace" "management" {
  metadata {
    name = "management"
    labels = {
      name = "management"
    }
  }
}

resource "kubernetes_persistent_volume_claim" "git_cache" {
  depends_on = [kubernetes_namespace.management]
  metadata {
    name      = "git-cache"
    namespace = "management"
  }

  spec {
    access_modes       = ["ReadWriteOnce"]
    storage_class_name = "gp3"

    resources {
      requests = {
        storage = "100Gi"
      }
    }
  }

  # Don't wait for PVC to be bound - gp3 storage class uses WaitForFirstConsumer
  # so the volume won't be provisioned until the deployment pod actually uses it
  wait_until_bound = false
}

resource "kubernetes_deployment" "git_cache" {
  metadata {
    name      = "git-cache"
    namespace = "management"
    labels = {
      app = "git-cache"
    }
  }

  # Don't wait for rollout - init container clones pytorch which takes 10-30 minutes
  wait_for_rollout = false

  spec {
    replicas = 1

    selector {
      match_labels = {
        app = "git-cache"
      }
    }

    template {
      metadata {
        labels = {
          app = "git-cache"
        }
      }

      spec {
        node_selector = {
          NodeType = "cpu"
        }

        toleration {
          key      = "node-role"
          operator = "Equal"
          value    = "cpu-only"
          effect   = "NoSchedule"
        }

        # Init: populate cache with pytorch + all submodules
        init_container {
          name              = "seed-cache"
          image             = "alpine/git:latest"
          image_pull_policy = "IfNotPresent"
          command           = ["/bin/sh", "-c"]
          args = [<<-EOT
            echo "[CACHE] Seeding git object cache..."

            # Mirror main repo
            REPO_DIR="/git-cache/pytorch.git"
            if [ -d "$REPO_DIR" ]; then
              echo "[CACHE] pytorch cache exists, refreshing..."
              cd "$REPO_DIR" && git remote update --prune || true
            else
              echo "[CACHE] Cold start - mirroring pytorch/pytorch..."
              git clone --mirror https://github.com/pytorch/pytorch.git "$REPO_DIR"
            fi

            # Mirror all submodules (parse .gitmodules from the cached repo)
            echo "[CACHE] Mirroring submodules..."
            cd "$REPO_DIR"
            git show HEAD:.gitmodules 2>/dev/null | grep 'url = ' | awk '{print $3}' | while read url; do
              # Derive cache dir name from URL: https://github.com/org/repo.git -> org_repo.git
              name=$(echo "$url" | sed 's|https://github.com/||;s|/|_|g;s|\.git$||').git
              sub_dir="/git-cache/$name"
              if [ -d "$sub_dir" ]; then
                echo "[CACHE]   Refreshing $name..."
                cd "$sub_dir" && git remote update --prune 2>/dev/null || true
              else
                echo "[CACHE]   Mirroring $name..."
                git clone --mirror "$url" "$sub_dir" 2>/dev/null || echo "[CACHE]   WARNING: Failed to mirror $url"
              fi
            done

            # Create git-daemon-export-ok files for all repos
            echo "[CACHE] Marking repos for git-daemon export..."
            for repo in /git-cache/*.git; do
              touch "$repo/git-daemon-export-ok"
            done

            echo "[CACHE] Seed complete"
          EOT
          ]

          volume_mount {
            name       = "git-cache"
            mount_path = "/git-cache"
          }
        }

        # HTTP server: serves pre-packaged tarballs (much faster than git-daemon)
        container {
          name              = "http-server"
          image             = "nginx:alpine"
          image_pull_policy = "IfNotPresent"

          command = ["/bin/sh", "-c"]
          args = [<<-EOT
            # Create nginx config for simple file serving
            cat > /etc/nginx/conf.d/default.conf << 'NGINXCONF'
server {
    listen 8080;
    server_name _;

    location / {
        root /git-cache;
        autoindex on;

        # CORS headers for cross-namespace access
        add_header Access-Control-Allow-Origin *;

        # Disable buffering for faster streaming
        proxy_buffering off;
        sendfile on;
        tcp_nopush on;
        tcp_nodelay on;
    }
}
NGINXCONF

            echo "[GIT-CACHE] Starting HTTP server on port 8080..."
            exec nginx -g 'daemon off;'
          EOT
          ]

          port {
            container_port = 8080
            name           = "http"
          }

          volume_mount {
            name       = "git-cache"
            mount_path = "/git-cache"
            read_only  = true
          }

          resources {
            requests = {
              cpu    = "200m"
              memory = "512Mi"
            }
            limits = {
              cpu    = "1000m"
              memory = "4Gi"
            }
          }

          liveness_probe {
            tcp_socket {
              port = 9418
            }
            initial_delay_seconds = 300
            period_seconds        = 60
          }
        }

        # Sidecar: refreshes cached repos and creates tarballs every hour
        container {
          name              = "cache-updater"
          image             = "alpine/git:latest"
          image_pull_policy = "IfNotPresent"
          command           = ["/bin/sh", "-c"]
          args = [<<-EOT
            # Install tar if not present
            apk add --no-cache tar pigz 2>/dev/null || true

            echo "[CACHE] Starting cache refresh loop (hourly)..."
            while true; do
              # Refresh git repos
              for repo in /git-cache/*.git; do
                if [ -d "$repo" ]; then
                  name=$(basename "$repo")
                  echo "[CACHE] Refreshing $name..."
                  cd "$repo"
                  git remote update --prune 2>&1 || echo "[CACHE] WARNING: Failed to refresh $name"
                fi
              done

              # Pick up any new submodules
              REPO_DIR="/git-cache/pytorch.git"
              if [ -d "$REPO_DIR" ]; then
                cd "$REPO_DIR"
                git show HEAD:.gitmodules 2>/dev/null | grep 'url = ' | awk '{print $3}' | while read url; do
                  name=$(echo "$url" | sed 's|https://github.com/||;s|/|_|g;s|\.git$||').git
                  sub_dir="/git-cache/$name"
                  if [ ! -d "$sub_dir" ]; then
                    echo "[CACHE] New submodule detected, mirroring $name..."
                    git clone --mirror "$url" "$sub_dir" 2>/dev/null || true
                  fi
                done

                # Create tarballs for main repo + ALL submodules
                # Naming convention: org_repo-git.tar.gz (matches git-clone-cached client)
                echo "[CACHE] Creating tarballs..."
                cd /git-cache

                # Main pytorch repo — name must match org_repo convention
                echo "[CACHE]   Creating pytorch_pytorch-git.tar.gz..."
                rm -f pytorch_pytorch-git.tar.gz.tmp
                tar -czf pytorch_pytorch-git.tar.gz.tmp -C /git-cache pytorch.git
                mv pytorch_pytorch-git.tar.gz.tmp pytorch_pytorch-git.tar.gz
                SIZE=$(du -sh pytorch_pytorch-git.tar.gz | awk '{print $1}')
                echo "[CACHE]   pytorch_pytorch: $SIZE"

                # All submodule repos (already named org_repo.git by init container)
                for repo in /git-cache/*.git; do
                  name=$(basename "$repo")
                  [ "$name" = "pytorch.git" ] && continue
                  tarball="$${name%.git}-git.tar.gz"
                  echo "[CACHE]   Creating $tarball..."
                  rm -f "$tarball.tmp" 2>/dev/null
                  tar -czf "$tarball.tmp" -C /git-cache "$name" 2>/dev/null && mv "$tarball.tmp" "$tarball" || echo "[CACHE]   WARNING: Failed to create $tarball"
                done
                echo "[CACHE] All tarballs created"

                # Build a ready-to-use worktree snapshot (master + submodules
                # checked out). Rebuild only when master advances.
                NEWSHA=$(git -C /git-cache/pytorch.git rev-parse HEAD 2>/dev/null || echo none)
                OLDSHA=$(cat /git-cache/pytorch-worktree-master.sha 2>/dev/null || echo never)
                if [ "$NEWSHA" = "$OLDSHA" ]; then
                  echo "[CACHE] Worktree snapshot already at $NEWSHA, skipping rebuild"
                else
                  echo "[CACHE] Building pytorch worktree snapshot ($OLDSHA -> $NEWSHA)..."
                  WT_DIR=/tmp/pytorch-worktree
                  rm -rf "$WT_DIR"
                  if git clone --no-hardlinks /git-cache/pytorch.git "$WT_DIR" 2>&1 | tail -1; then
                    cd "$WT_DIR"
                    git checkout -f master 2>/dev/null || git checkout -f main 2>/dev/null || true

                    # Point top-level submodule URLs at local mirrors for a network-free update
                    git submodule init 2>/dev/null || true
                    git config -f .gitmodules --get-regexp '^submodule\..*\.url$' | while read key url; do
                      name=$(echo "$url" | sed 's|https://github.com/||;s|/|_|g;s|\.git$||')
                      sub=$(echo "$key" | sed 's/^submodule\.//;s/\.url$//')
                      mirror="/git-cache/$name.git"
                      [ -d "$mirror" ] && git config "submodule.$sub.url" "file://$mirror"
                    done

                    echo "[CACHE]   Checking out submodules..."
                    git -c protocol.file.allow=always submodule update --init --recursive 2>&1 | tail -3 || echo "[CACHE]   WARNING: some submodules failed"

                    # Restore all origins to GitHub so user git ops work after drop-in
                    git remote set-url origin https://github.com/pytorch/pytorch.git
                    git config -f .gitmodules --get-regexp '^submodule\..*\.url$' | while read key url; do
                      sub=$(echo "$key" | sed 's/^submodule\.//;s/\.url$//')
                      git config "submodule.$sub.url" "$url"
                    done
                    git -c protocol.file.allow=always submodule foreach --recursive 'u=$(git config -f "$toplevel/.gitmodules" "submodule.$name.url" 2>/dev/null); [ -n "$u" ] && git remote set-url origin "$u" || true' 2>/dev/null || true

                    echo "[CACHE]   Packaging worktree @ $NEWSHA..."
                    cd /tmp
                    rm -f /git-cache/pytorch-worktree-master.tar.gz.tmp
                    if tar -C /tmp -cf - pytorch-worktree | pigz -p 4 > /git-cache/pytorch-worktree-master.tar.gz.tmp 2>/dev/null; then
                      mv /git-cache/pytorch-worktree-master.tar.gz.tmp /git-cache/pytorch-worktree-master.tar.gz
                      echo "$NEWSHA" > /git-cache/pytorch-worktree-master.sha
                      WTSIZE=$(du -sh /git-cache/pytorch-worktree-master.tar.gz | awk '{print $1}')
                      echo "[CACHE]   pytorch-worktree-master.tar.gz: $WTSIZE @ $NEWSHA"
                    else
                      echo "[CACHE]   WARNING: worktree packaging failed"
                      rm -f /git-cache/pytorch-worktree-master.tar.gz.tmp
                    fi
                    rm -rf "$WT_DIR"
                  else
                    echo "[CACHE]   WARNING: worktree clone failed"
                  fi
                fi
              fi

              echo "[CACHE] Refresh complete at $(date). Next in 3600s (1 hour)..."
              sleep 3600
            done
          EOT
          ]

          volume_mount {
            name       = "git-cache"
            mount_path = "/git-cache"
          }

          resources {
            requests = {
              cpu    = "500m"
              memory = "2Gi"
            }
            limits = {
              cpu    = "2000m"
              memory = "8Gi"
            }
          }
        }

        volume {
          name = "git-cache"
          persistent_volume_claim {
            claim_name = kubernetes_persistent_volume_claim.git_cache.metadata[0].name
          }
        }
      }
    }
  }
}

resource "kubernetes_service" "git_cache" {
  metadata {
    name      = "git-cache"
    namespace = "management"
    labels = {
      app = "git-cache"
    }
  }

  spec {
    type = "ClusterIP"

    port {
      port        = 8080
      target_port = 8080
      protocol    = "TCP"
      name        = "http"
    }

    selector = {
      app = "git-cache"
    }
  }
}

# DaemonSet: keep a node-local copy of the pytorch worktree snapshot on each
# dev node's NVMe (falls back to root disk via DirectoryOrCreate). Pods then
# drop pytorch in with a local copy instead of pulling the tarball from the
# in-cluster cache every time. Refreshes when the snapshot sha changes.
resource "kubernetes_daemonset" "pytorch_snapshot" {
  metadata {
    name      = "pytorch-snapshot"
    namespace = "kube-system"
    labels    = { app = "pytorch-snapshot" }
  }

  spec {
    selector {
      match_labels = { app = "pytorch-snapshot" }
    }

    strategy {
      type = "RollingUpdate"
      rolling_update {
        max_unavailable = "100%"
      }
    }

    template {
      metadata {
        labels = { app = "pytorch-snapshot" }
      }

      spec {
        # GPU + CPU dev nodes only (skip mgmt/control-plane).
        affinity {
          node_affinity {
            required_during_scheduling_ignored_during_execution {
              node_selector_term {
                match_expressions {
                  key      = "NodeType"
                  operator = "In"
                  values   = ["gpu", "cpu"]
                }
              }
            }
          }
        }

        toleration {
          key      = "nvidia.com/gpu"
          operator = "Exists"
          effect   = "NoSchedule"
        }
        toleration {
          key      = "node-role"
          operator = "Equal"
          value    = "cpu-only"
          effect   = "NoSchedule"
        }

        container {
          name    = "snapshot"
          image   = "alpine:3.21"
          command = ["/bin/sh", "-c"]
          args = [<<-EOT
            apk add --no-cache curl tar >/dev/null 2>&1 || true
            CACHE="http://git-cache.management.svc.cluster.local:8080"
            DEST=/mnt/nvme/pytorch-worktree
            ARCH=$(uname -m)
            PREBUILT="/ccache_shared/prebuilt/pytorch-$ARCH"
            BUILT=/mnt/nvme/pytorch-built
            echo "[nvme-pytorch] snapshot maintainer started (arch=$ARCH)"
            while true; do
              # 1. source-only worktree snapshot (master) from git-cache HTTP —
              #    used for --ref / build-from-scratch staging.
              NEW=$(curl -sf "$CACHE/pytorch-worktree-master.sha" 2>/dev/null || echo none)
              OLD=$(cat "$DEST/.sha" 2>/dev/null || echo never)
              if [ "$NEW" != "none" ] && [ "$NEW" != "$OLD" ]; then
                echo "[nvme-pytorch] worktree $OLD -> $NEW"
                rm -rf /mnt/nvme/pytorch-worktree.tmp
                mkdir -p /mnt/nvme/pytorch-worktree.tmp
                if curl -sf "$CACHE/pytorch-worktree-master.tar.gz" | tar -xz -C /mnt/nvme/pytorch-worktree.tmp --strip-components=1; then
                  echo "$NEW" > /mnt/nvme/pytorch-worktree.tmp/.sha
                  rm -rf /mnt/nvme/pytorch-worktree.old
                  [ -d "$DEST" ] && mv "$DEST" /mnt/nvme/pytorch-worktree.old
                  mv /mnt/nvme/pytorch-worktree.tmp "$DEST"
                  rm -rf /mnt/nvme/pytorch-worktree.old
                  echo "[nvme-pytorch] worktree ready at $NEW"
                else
                  echo "[nvme-pytorch] worktree download failed, will retry"
                  rm -rf /mnt/nvme/pytorch-worktree.tmp
                fi
              fi

              # 2. prebuilt viable/strict tree (source + build/ + .so, importable)
              #    from the shared EFS, published as a single zstd tarball (rsync of
              #    the raw tree over EFS/NFS dies on per-file round-trips). Download
              #    the tarball (sequential read) + extract to node-local NVMe.
              if [ -f "$PREBUILT.sha" ]; then
                BNEW=$(cat "$PREBUILT.sha" 2>/dev/null || echo none)
                BOLD=$(cat "$BUILT/.sha" 2>/dev/null || echo never)
                if [ "$BNEW" != "$BOLD" ] && { [ -f "$PREBUILT.tar.zst" ] || [ -f "$PREBUILT.tar.gz" ]; }; then
                  echo "[nvme-pytorch] built tree $BOLD -> $BNEW"
                  rm -rf /mnt/nvme/pytorch-built.tmp
                  mkdir -p /mnt/nvme/pytorch-built.tmp
                  if [ -f "$PREBUILT.tar.zst" ]; then
                    apk add --no-cache zstd >/dev/null 2>&1 || true
                    DECOMP="zstd -dc $PREBUILT.tar.zst"
                  else
                    DECOMP="gzip -dc $PREBUILT.tar.gz"
                  fi
                  if $DECOMP | tar -x -C /mnt/nvme/pytorch-built.tmp --strip-components=1; then
                    echo "$BNEW" > /mnt/nvme/pytorch-built.tmp/.sha
                    rm -rf /mnt/nvme/pytorch-built.old
                    [ -d "$BUILT" ] && mv "$BUILT" /mnt/nvme/pytorch-built.old
                    mv /mnt/nvme/pytorch-built.tmp "$BUILT"
                    rm -rf /mnt/nvme/pytorch-built.old
                    echo "[nvme-pytorch] built tree ready at $BNEW"
                  else
                    echo "[nvme-pytorch] built tree extract failed, will retry"
                    rm -rf /mnt/nvme/pytorch-built.tmp
                  fi
                fi
              fi

              sleep 900
            done
          EOT
          ]

          volume_mount {
            name       = "nvme-root"
            mount_path = "/mnt/nvme"
          }
          volume_mount {
            name       = "ccache-shared"
            mount_path = "/ccache_shared"
            read_only  = true
          }

          resources {
            requests = {
              cpu    = "50m"
              memory = "64Mi"
            }
            limits = {
              cpu    = "500m"
              memory = "512Mi"
            }
          }
        }

        volume {
          name = "nvme-root"
          host_path {
            path = "/mnt/nvme"
            type = "DirectoryOrCreate"
          }
        }
        # Shared ccache EFS — source of the prebuilt viable/strict tree (/prebuilt).
        volume {
          name = "ccache-shared"
          nfs {
            server    = local.ccache_efs_dns
            path      = "/"
            read_only = true
          }
        }
      }
    }
  }
}
