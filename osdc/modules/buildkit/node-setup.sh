#!/bin/bash
set -euo pipefail

# ---- NVMe RAID0 setup ----
echo "Post-bootstrap: Setting up NVMe RAID0..."

# Find NVMe instance store devices (exclude root EBS)
NVME_DEVICES=()
for dev in /dev/nvme*n1; do
  # Skip if it's the root device (has partitions or is mounted)
  if lsblk "$dev" 2>/dev/null | grep -q "part\|/"; then
    continue
  fi
  NVME_DEVICES+=("$dev")
done

if [ ${#NVME_DEVICES[@]} -gt 0 ]; then
  echo "Found ${#NVME_DEVICES[@]} NVMe instance store devices: ${NVME_DEVICES[*]}"

  if [ ${#NVME_DEVICES[@]} -gt 1 ]; then
    # Create RAID0 array
    yum install -y mdadm 2>/dev/null || true
    mdadm --create /dev/md0 --level=0 --raid-devices=${#NVME_DEVICES[@]} "${NVME_DEVICES[@]}" --force --run
    mkfs.xfs -f /dev/md0
    mount /dev/md0 /mnt
  else
    # Single device
    mkfs.xfs -f "${NVME_DEVICES[0]}"
    mount "${NVME_DEVICES[0]}" /mnt
  fi

  # Create data directories on NVMe
  mkdir -p /mnt/git-cache /mnt/buildkit-cache
  chmod 755 /mnt/git-cache /mnt/buildkit-cache
  echo "NVMe mounted at /mnt ($(df -h /mnt | tail -1 | awk '{print $2}') total)"
else
  echo "No NVMe instance store devices found, using EBS"
  mkdir -p /mnt/git-cache /mnt/buildkit-cache
fi

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
  set -- $registry_project
  registry=$1; project=$2; upstream=$3
  mkdir -p /etc/containerd/certs.d/$registry
  cat > /etc/containerd/certs.d/$registry/hosts.toml <<MIRRORS
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
echo "Post-bootstrap: Configuring CPU performance settings..."
for cpu_governor in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do
  if [ -f "$cpu_governor" ]; then
    echo "performance" > "$cpu_governor" || true
  fi
done

cat > /etc/systemd/system/cpu-performance.service <<'EOFS'
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

systemctl daemon-reload
systemctl enable cpu-performance.service
systemctl start cpu-performance.service

echo "Performance configuration complete"
