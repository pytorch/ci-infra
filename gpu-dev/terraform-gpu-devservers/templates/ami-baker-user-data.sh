#!/bin/bash
set -e
echo "[AMI-BAKER] Starting GPU AMI build..."

# Install NVIDIA driver (compiles kernel modules — the 13-min step we're eliminating)
echo "[AMI-BAKER] Installing NVIDIA drivers..."
echo "options nvidia NVreg_RestrictProfilingToAdminUsers=0" > /etc/modprobe.d/nvprof.conf
dnf config-manager --add-repo https://developer.download.nvidia.com/compute/cuda/repos/amzn2023/x86_64/cuda-amzn2023.repo
dnf install -y nvidia-driver nvidia-driver-cuda
echo "[AMI-BAKER] NVIDIA driver installed"

# Install fabricmanager + infiniband tools (won't start without NVSwitch but binaries are on disk)
echo "[AMI-BAKER] Installing fabricmanager and infiniband tools..."
dnf install -y nvidia-fabricmanager nvlsm infiniband-diags 2>/dev/null || echo "fabricmanager install warning (non-fatal)"
systemctl enable nvidia-fabricmanager 2>/dev/null || true
echo "[AMI-BAKER] fabricmanager installed"

# Load NVIDIA modules (creates device files needed by containerd)
modprobe nvidia 2>/dev/null || echo "nvidia module load skipped (no GPU — expected during AMI build)"
modprobe nvidia_uvm 2>/dev/null || echo "nvidia_uvm load skipped"

# Pull the Docker image into containerd cache
echo "[AMI-BAKER] Pulling Docker image into containerd cache..."
IMAGE_URI="${image_uri}"
ECR_REGION=$(echo "$IMAGE_URI" | cut -d. -f4)

# Wait for containerd to be ready
for i in $(seq 1 30); do
  ctr version >/dev/null 2>&1 && break
  echo "[AMI-BAKER] Waiting for containerd..."
  sleep 2
done

# Get ECR auth token and pull
ECR_TOKEN=$(aws ecr get-login-password --region "$ECR_REGION" 2>/dev/null || echo "")
if [ -n "$ECR_TOKEN" ]; then
  ctr -n k8s.io images pull --user "AWS:$ECR_TOKEN" "$IMAGE_URI" 2>&1 || echo "[AMI-BAKER] Image pull failed (non-fatal — will pull at boot)"
  echo "[AMI-BAKER] Docker image cached"
else
  echo "[AMI-BAKER] No ECR token — skipping image cache"
fi

# Pre-pull GPU Operator DaemonSet images (saves ~10 min on cold boot)
# These are the images GPU Operator v25.3.3 schedules on every GPU node
echo "[AMI-BAKER] Pre-pulling GPU Operator images..."
for IMG in \
  nvcr.io/nvidia/k8s/container-toolkit:v1.17.8-ubuntu20.04 \
  nvcr.io/nvidia/k8s-device-plugin:v0.17.4 \
  nvcr.io/nvidia/cloud-native/dcgm:4.3.1-1-ubuntu22.04 \
  nvcr.io/nvidia/k8s/dcgm-exporter:4.3.1-4.4.0-ubuntu22.04 \
  nvcr.io/nvidia/cloud-native/k8s-mig-manager:v0.12.3-ubuntu20.04 \
  docker.io/alpine:3.21; do
  echo "[AMI-BAKER] Pulling $IMG..."
  ctr -n k8s.io images pull "$IMG" 2>&1 || echo "[AMI-BAKER] Failed to pull $IMG (non-fatal)"
done
echo "[AMI-BAKER] GPU Operator images cached"

# Signal completion
echo "[AMI-BAKER] Build complete" > /tmp/ami-baker-done
echo "[AMI-BAKER] AMI build complete"
