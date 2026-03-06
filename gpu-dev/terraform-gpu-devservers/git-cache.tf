# Git Cache Service - In-cluster object cache for fast git clone operations
# Maintains bare copies of pytorch/pytorch AND all its submodules, refreshed every 15 min.
# User pods get a transparent git wrapper that clones from cache,
# then sets origin to GitHub — all subsequent git ops go to GitHub directly.

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

                # Create bare .git tarball (much faster - no checkout needed!)
                echo "[CACHE] Creating pytorch .git tarball..."
                cd /git-cache
                rm -f pytorch-git.tar.gz.tmp

                # Just tar up the bare repo (pack files only, no working tree)
                # Client will do git checkout after download (unavoidable anyway)
                tar -czf pytorch-git.tar.gz.tmp -C /git-cache pytorch.git
                mv pytorch-git.tar.gz.tmp pytorch-git.tar.gz

                SIZE=$(du -sh pytorch-git.tar.gz | awk '{print $1}')
                echo "[CACHE] Bare .git tarball created: $SIZE"

                # Create tarballs for largest submodules (top 10 by size)
                echo "[CACHE] Creating submodule tarballs..."
                for repo in $(du -s /git-cache/*.git 2>/dev/null | sort -rn | head -11 | tail -10 | awk '{print $2}'); do
                  name=$(basename "$repo")
                  tarball="$${name%.git}-git.tar.gz"
                  echo "[CACHE]   Creating $tarball..."
                  rm -f "$tarball.tmp" 2>/dev/null
                  tar -czf "$tarball.tmp" -C /git-cache "$name" 2>/dev/null && mv "$tarball.tmp" "$tarball" || echo "[CACHE]   WARNING: Failed to create $tarball"
                done
                echo "[CACHE] Submodule tarballs created"
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
