#!/bin/bash
set -euo pipefail

# Retry helper: 3 attempts with 30s/90s backoff
retry() {
  local max_attempts=3
  local attempt=1
  local backoff
  while [ $attempt -le $max_attempts ]; do
    if "$@"; then return 0; fi
    if [ $attempt -eq $max_attempts ]; then
      echo "FATAL: Command failed after $max_attempts attempts: $*" >&2
      return 1
    fi
    backoff=$((attempt == 1 ? 30 : 90))
    echo "WARN: Attempt $attempt/$max_attempts failed, retrying in ${backoff}s: $*" >&2
    sleep $backoff
    attempt=$((attempt + 1))
  done
}

# ---- NVMe storage setup ----
# Primary: nodeadm's instanceStorePolicy: RAID0 handles NVMe formatting and
# mounts at /mnt/k8s-disks/0/. It also bind-mounts containerd + kubelet there.
# Fallback: if nodeadm didn't set it up, do RAID0 manually (legacy path).

NVME_MNT="/mnt/k8s-disks/0"

if mountpoint -q "$NVME_MNT" 2>/dev/null; then
  echo "NVMe mounted at $NVME_MNT by nodeadm localStorage"
  DATA_DIR="$NVME_MNT"
else
  echo "WARN: NVMe not mounted at $NVME_MNT — falling back to manual RAID0 setup" >&2

  # ---- Legacy NVMe RAID0 setup ----
  echo "Post-bootstrap: Setting up NVMe RAID0..."

  # Wait for device nodes to stabilize (early boot race)
  udevadm settle 2>/dev/null || true

  # Find NVMe instance store devices (exclude root EBS)
  shopt -s nullglob
  NVME_DEVICES=()

  ROOT_DEV=$(lsblk -ndo PKNAME "$(findmnt -n -o SOURCE /)" 2>/dev/null || echo "")
  if [ -z "$ROOT_DEV" ]; then
    echo "WARN: Could not detect root device via findmnt, falling back to lsblk heuristic" >&2
  fi

  for dev in /dev/nvme*n1; do
    # Skip root device if detected via findmnt
    if [ -n "$ROOT_DEV" ] && [ "$(basename "$dev")" = "$ROOT_DEV" ]; then continue; fi
    # Fallback: skip if it has partitions or is mounted (if lsblk fails, skip device to be safe)
    if ! lsblk "$dev" >/dev/null 2>&1 || lsblk "$dev" 2>/dev/null | grep -q "part\|/"; then continue; fi
    NVME_DEVICES+=("$dev")
  done

  if [ ${#NVME_DEVICES[@]} -gt 0 ]; then
    echo "Found ${#NVME_DEVICES[@]} NVMe instance store devices: ${NVME_DEVICES[*]}"

    if [ ${#NVME_DEVICES[@]} -gt 1 ]; then
      # Attempt RAID0; degrade to single device if mdadm install or create fails
      if retry yum install -y mdadm \
        && retry mdadm --create /dev/md0 --level=0 --raid-devices=${#NVME_DEVICES[@]} "${NVME_DEVICES[@]}" --force --run; then
        mkfs.xfs -f /dev/md0
        MOUNT_DEV=/dev/md0
      else
        echo "WARN: RAID0 setup failed, falling back to single NVMe device" >&2
        mdadm --stop /dev/md0 2>/dev/null || true
        mkfs.xfs -f "${NVME_DEVICES[0]}"
        MOUNT_DEV="${NVME_DEVICES[0]}"
      fi
    else
      # Single device
      mkfs.xfs -f "${NVME_DEVICES[0]}"
      MOUNT_DEV="${NVME_DEVICES[0]}"
    fi

    if mountpoint -q /mnt; then
      echo "WARN: /mnt already mounted, skipping mount" >&2
    else
      mount "$MOUNT_DEV" /mnt
    fi
    echo "NVMe mounted at /mnt ($(df -h /mnt | tail -1 | awk '{print $2}') total)"
  else
    echo "No NVMe instance store devices found, using EBS"
  fi

  DATA_DIR="/mnt"

  # Create compatibility symlink so hostPaths at /mnt/k8s-disks/0/* resolve
  if [ ! -e "$NVME_MNT" ]; then
    mkdir -p "$(dirname "$NVME_MNT")"
    ln -s /mnt "$NVME_MNT"
    echo "Created compatibility symlink: $NVME_MNT -> /mnt"
  fi
fi

# Create application directories on NVMe (or EBS fallback)
mkdir -p "$DATA_DIR/git-cache" "$DATA_DIR/buildkit-cache"
chmod 755 "$DATA_DIR/git-cache" "$DATA_DIR/buildkit-cache"
echo "Data directories ready at $DATA_DIR"
