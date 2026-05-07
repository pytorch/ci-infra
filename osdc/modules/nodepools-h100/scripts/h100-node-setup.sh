#!/bin/bash
set -euo pipefail

# ---- Registry mirror configuration (Harbor pull-through cache) ----
echo "Post-bootstrap: Configuring registry mirrors..."
HARBOR_PORT=30002

for registry_project in \
  "docker.io dockerhub-cache https://docker.io" \
  "ghcr.io ghcr-cache https://ghcr.io" \
  "public.ecr.aws ecr-public-cache https://public.ecr.aws" \
  "nvcr.io nvcr-cache https://nvcr.io" \
  "registry.k8s.io k8s-cache https://registry.k8s.io" \
  "quay.io quay-cache https://quay.io"; do
  # shellcheck disable=SC2086  # Intentional word-splitting
  set -- $registry_project
  registry=$1
  project=$2
  upstream=$3
  mkdir -p "/etc/containerd/certs.d/$registry"
  cat >"/etc/containerd/certs.d/$registry/hosts.toml" <<MIRRORS
server = "$upstream"

[host."http://localhost:$HARBOR_PORT/v2/$project"]
  capabilities = ["pull", "resolve"]
  skip_verify = true
  override_path = true

[host."$upstream"]
  capabilities = ["pull", "resolve"]
MIRRORS
done
echo "Registry mirrors configured for 6 registries"

# ---- CPU performance tuning ----
echo "Post-bootstrap: Configuring performance settings for p5.48xlarge..."

for cpu_governor in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do
  if [ -f "$cpu_governor" ]; then
    echo "performance" >"$cpu_governor" || true
  fi
done

cat >/etc/systemd/system/cpu-performance.service <<'EOFS'
[Unit]
Description=Set CPU governor to performance mode
After=multi-user.target

[Service]
Type=oneshot
ExecStart=/bin/bash -c \
  'for gov in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; \
  do echo performance > $gov 2>/dev/null || true; done'
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOFS

# daemon-reload + enable only — do NOT 'systemctl start' here.
# This script runs inside cloud-init (cloud-final.service), and the service
# unit has After=multi-user.target, creating a systemd deadlock:
#   cloud-final waits for this script → script waits for systemctl start →
#   systemd waits for multi-user.target → multi-user.target waits for cloud-final.
# The CPU governors are already set directly above; the service is only
# needed for persistence across reboots.
systemctl daemon-reload
systemctl enable cpu-performance.service

# Enable GPU persistence mode for consistent performance
nvidia-smi -pm 1 || true

# ---- Fabric Manager + IMEX channel (required for NCCL NVLS multicast) ----
# NVSwitch-connected multi-GPU nodes (p5.48xlarge) need the Fabric Manager
# running and the IMEX channel device available for NCCL to use NVLS.
# Without these, any multi-process NCCL collective will fail with:
#   "Failed to bind NVLink SHARP (NVLS) Multicast memory"
cat >/etc/modules-load.d/nvidia-caps-imex-channels.conf <<'EOFS'
nvidia-caps-imex-channels
EOFS

systemctl enable nvidia-fabricmanager || {
  echo "ERROR: failed to enable nvidia-fabricmanager" >&2
  exit 1
}

systemctl start nvidia-fabricmanager || {
  systemctl status nvidia-fabricmanager --no-pager || true
  echo "ERROR: failed to start nvidia-fabricmanager" >&2
  exit 1
}

systemctl is-active --quiet nvidia-fabricmanager || {
  systemctl status nvidia-fabricmanager --no-pager || true
  echo "ERROR: nvidia-fabricmanager is not active" >&2
  exit 1
}

modprobe nvidia-caps-imex-channels || {
  echo "ERROR: failed to load nvidia-caps-imex-channels" >&2
  exit 1
}

if ! lsmod | grep -q '^nvidia_caps_imex_channels '; then
  echo "ERROR: nvidia-caps-imex-channels is not loaded" >&2
  exit 1
fi

echo "Performance configuration complete for p5.48xlarge"
