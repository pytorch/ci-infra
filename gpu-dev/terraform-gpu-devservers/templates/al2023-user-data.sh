#!/bin/bash

# Simple Amazon Linux 2023 EKS node setup
# Remove any existing NVIDIA drivers and let GPU Operator manage them

set -o xtrace

# Disable the default nodeadm services that try to parse user-data as config
systemctl disable nodeadm-config.service || true
systemctl disable nodeadm-run.service || true
systemctl stop nodeadm-config.service || true
systemctl stop nodeadm-run.service || true

# Install latest NVIDIA driver on host (595.x branch supports CUDA 13.2)
# GPU Operator will handle toolkit/device-plugin only

# Configure NVIDIA profiling BEFORE driver installation (driver install auto-loads modules)
# Required for ncu/nsys GPU profiling tools
echo "options nvidia NVreg_RestrictProfilingToAdminUsers=0" > /etc/modprobe.d/nvprof.conf

# Skip driver install if baked AMI already has them (saves ~13 min)
if rpm -q nvidia-driver &>/dev/null; then
    echo "NVIDIA driver already installed (baked AMI) — skipping dnf install"
else
    echo "Installing NVIDIA driver from scratch..."
    dnf config-manager --add-repo https://developer.download.nvidia.com/compute/cuda/repos/amzn2023/x86_64/cuda-amzn2023.repo
    dnf install -y nvidia-driver nvidia-driver-cuda
fi

# EFA driver update + efa-nv-peermem for GPU Direct RDMA (GDR)
# EFA 2.17.3+ adds P2P support for NVIDIA 580 open drivers
# efa-nv-peermem bridges EFA and NVIDIA for direct GPU memory transfers (bypasses CPU copies)
dnf install -y cmake kernel-devel-$(uname -r) gcc dkms 2>/dev/null || echo "Build deps install warning (non-fatal)"
if [ -d /sys/class/infiniband ] || lspci | grep -qi "EFA"; then
    echo "EFA hardware detected - updating EFA driver and building efa-nv-peermem..."

    # Update EFA kernel driver to latest via EFA installer (gets 2.17.3+ with P2P support)
    cd /tmp
    EFA_VERSION=1.47.0
    curl -sL https://efa-installer.amazonaws.com/aws-efa-installer-$${EFA_VERSION}.tar.gz | tar xz
    cd aws-efa-installer
    ./efa_installer.sh -y -g --skip-limit-conf --no-verify 2>&1 || echo "EFA installer warning (non-fatal)"
    cd /tmp && rm -rf aws-efa-installer*

    # Reload EFA kernel module to pick up DKMS-built version (old one loaded at boot)
    rmmod efa 2>/dev/null && modprobe efa 2>/dev/null || echo "EFA module reload skipped (in use or unchanged)"

    # Build efa-nv-peermem from amzn-drivers source
    cd /tmp
    git clone --depth 1 https://github.com/amzn/amzn-drivers.git
    cd amzn-drivers/kernel/linux/efa_nv_peermem
    mkdir -p build && cd build
    cmake .. 2>&1 && make 2>&1
    if [ -f src/efa_nv_peermem.ko ]; then
        cp src/efa_nv_peermem.ko /lib/modules/$(uname -r)/extra/
        depmod -a
        echo "efa-nv-peermem module built successfully"
    else
        echo "efa-nv-peermem build failed - GDR will be disabled"
    fi
    cd /tmp && rm -rf amzn-drivers
else
    echo "No EFA hardware detected - skipping EFA update and efa-nv-peermem build"
fi

# Install fabric manager for multi-GPU NVSwitch systems (A100-SXM4, B200, H200, H100)
# Fabric manager is required for proper CUDA initialization on these GPUs
if [[ "${gpu_type}" == "a100" || "${gpu_type}" == "b200" || "${gpu_type}" == "b300" || "${gpu_type}" == "h200" || "${gpu_type}" == "h100" ]]; then
    echo "Installing fabric manager for multi-GPU system: ${gpu_type}"

    # Install InfiniBand tools - EFA hardware is already present and configured
    if ! rpm -q infiniband-diags &>/dev/null; then
        echo "Installing InfiniBand diagnostic tools for fabric manager..."
        dnf install -y infiniband-diags
    fi

    # Install fabric manager and NVLink Subnet Manager
    if ! rpm -q nvidia-fabricmanager &>/dev/null; then
        dnf install -y nvidia-fabricmanager nvlsm
    fi

    # Fix PATH issue - create symlink for ibstat in /usr/bin where fabric manager expects it
    ln -sf /usr/sbin/ibstat /usr/bin/ibstat || echo "ibstat symlink creation failed"

    # Load required InfiniBand kernel module for fabric manager
    modprobe ib_umad || echo "ib_umad module load failed"

    # Always start fabric manager for B200/H200/H100 - required for CUDA initialization
    echo "Starting fabric manager (required for CUDA error 802 fix with EFA)"
    systemctl unmask nvidia-fabricmanager.service
    systemctl enable nvidia-fabricmanager

    # Create run directory if it doesn't exist
    mkdir -p /run/nvidia-fabricmanager

    # Start fabric manager - should work now with ibstat in PATH
    systemctl start nvidia-fabricmanager || echo "Fabric manager start returned non-zero, checking status..."

    # Show status for debugging
    systemctl status nvidia-fabricmanager --no-pager || true

    # Enable persistent mode as well
    nvidia-smi -pm 1 || echo "Could not enable persistent mode"

    echo "Fabric manager setup completed for ${gpu_type} with EFA support"
fi

# Load NVIDIA modules (profiling config already set above before driver install)
modprobe nvidia
modprobe nvidia_uvm
# Enable GPU Direct RDMA for EFA - allows NCCL to transfer GPU memory directly over EFA
# Try efa-nv-peermem first (built from amzn-drivers, for EFA), fallback to nvidia-peermem (for IB)
modprobe efa-nv-peermem 2>/dev/null || insmod /lib/modules/$(uname -r)/extra/efa_nv_peermem.ko 2>/dev/null || modprobe nvidia-peermem 2>/dev/null || echo "No peermem module available (GDR disabled)"

# Initialize NVIDIA device files - required for device plugin to detect GPUs
# This creates /dev/nvidia*, /dev/nvidiactl, /dev/nvidia-uvm etc.
nvidia-smi -pm 1 || echo "Could not enable persistent mode (device files still created)"

# Install basic monitoring tools
yum install -y htop wget

# ── Mount local NVMe instance store for containerd image cache ──
# GPU instances (p5, p6, g4dn, g6, g7e) have fast local NVMe SSDs that sit idle.
# Moving containerd's root here makes image pulls instant from cache and avoids
# EBS IOPS contention with user workloads.
NVME_MOUNT="/mnt/nvme"
NVME_DEVS=()
for dev in /dev/nvme*n1; do
    [ -b "$dev" ] || continue
    # Instance store NVMe has model "Amazon EC2 NVMe Instance Storage"
    # EBS NVMe has model "Amazon Elastic Block Store"
    DEV_NAME=$(basename "$dev")
    MODEL=$(cat /sys/block/$${DEV_NAME}/device/model 2>/dev/null | tr -d ' ')
    if echo "$MODEL" | grep -qi "Instance"; then
        NVME_DEVS+=("$dev")
    fi
done

if [ $${#NVME_DEVS[@]} -gt 0 ]; then
    echo "Found $${#NVME_DEVS[@]} local NVMe device(s): $${NVME_DEVS[*]}"
    mkdir -p "$NVME_MOUNT"

    if [ $${#NVME_DEVS[@]} -eq 1 ]; then
        mkfs.xfs -f "$${NVME_DEVS[0]}"
        mount "$${NVME_DEVS[0]}" "$NVME_MOUNT"
    else
        # RAID0 across all NVMe devices for maximum throughput
        dnf install -y mdadm 2>/dev/null || yum install -y mdadm 2>/dev/null
        mdadm --create /dev/md0 --level=0 --raid-devices=$${#NVME_DEVS[@]} "$${NVME_DEVS[@]}" --force --run
        mkfs.xfs -f /dev/md0
        mount /dev/md0 "$NVME_MOUNT"
    fi

    # Move baked AMI's containerd cache to NVMe, then bind-mount
    # Stop containerd first to avoid corrupting boltdb during copy
    systemctl stop containerd 2>/dev/null || true
    mkdir -p "$NVME_MOUNT/containerd"
    if [ -d /var/lib/containerd ] && [ "$(ls -A /var/lib/containerd 2>/dev/null)" ]; then
        echo "Copying baked containerd cache to NVMe..."
        cp -a /var/lib/containerd/* "$NVME_MOUNT/containerd/" 2>/dev/null || true
    fi
    mkdir -p /var/lib/containerd
    mount --bind "$NVME_MOUNT/containerd" /var/lib/containerd
    # nodeadm will restart containerd with proper config

    NVME_SIZE=$(df -h "$NVME_MOUNT" | awk 'NR==2{print $2}')
    echo "NVMe mounted at $NVME_MOUNT ($NVME_SIZE) — containerd image cache on local SSD"
    NVME_LABEL=",nvme-cache=true"
else
    echo "No local NVMe instance store found — using EBS root for containerd"
    NVME_LABEL=""
fi

# Configure and run nodeadm for EKS cluster joining
# Get the base64 certificate data from AWS
CA_DATA=$(aws eks describe-cluster --region ${region} --name ${cluster_name} --query 'cluster.certificateAuthority.data' --output text)

cat > /tmp/nodeadm-config.yaml <<EOF
apiVersion: node.eks.aws/v1alpha1
kind: NodeConfig
spec:
  cluster:
    name: ${cluster_name}
    apiServerEndpoint: ${cluster_endpoint}
    certificateAuthority: $CA_DATA
    cidr: 172.20.0.0/16
  kubelet:
    config:
      clusterDNS:
        - 172.20.0.10
      cpuManagerPolicy: static
      cpuManagerReconcilePeriod: 10s
      systemReserved:
        cpu: "2"
        memory: "4Gi"
      kubeReserved:
        cpu: "2"
        memory: "4Gi"
    flags:
      - --node-labels=NodeType=gpu,GpuType=${gpu_type},nvidia.com/gpu.deploy.driver=false${profiling_dedicated ? ",gpu.monitoring/profiling-dedicated=true,nvidia.com/gpu.deploy.dcgm-exporter=false" : ""}${mig_profile != "" ? ",nvidia.com/mig.config=${mig_profile}" : ""}$NVME_LABEL
EOF

# Configure EFA if hardware present (BEFORE nodeadm so kubelet sees hugepages)
if [[ -d /sys/class/infiniband/efa_0 || -e /dev/infiniband/uverbs0 ]]; then
    echo 'FI_PROVIDER=efa' >> /etc/environment
    echo 'NCCL_PROTO=simple' >> /etc/environment

    # Allocate huge pages for EFA RDMA - required for cross-node communication
    # Must happen BEFORE nodeadm/kubelet starts so hugepages are reported as allocatable
    echo 5128 > /proc/sys/vm/nr_hugepages
    echo "vm.nr_hugepages = 5128" > /etc/sysctl.d/90-efa-hugepages.conf
fi

/usr/bin/nodeadm init --config-source file:///tmp/nodeadm-config.yaml

# Network tuning
cat >/etc/sysctl.d/99-gpu-net.conf <<'EOF'
net.core.rmem_default=262144000
net.core.rmem_max=262144000
net.core.wmem_default=262144000
net.core.wmem_max=262144000
EOF
sysctl --system

# Pre-pull GPU dev container image in background (after nodeadm finishes)
ECR_IMAGE="${container_image}"
(
  # Wait for crictl to be available (nodeadm installs it)
  for i in $(seq 1 60); do
    command -v crictl &>/dev/null && break
    [ -x /usr/local/bin/crictl ] && export PATH=/usr/local/bin:$PATH && break
    sleep 5
  done
  # Wait for containerd socket
  for i in $(seq 1 30); do
    crictl version &>/dev/null && break
    sleep 2
  done
  # Check if baked AMI image survived nodeadm restart
  CACHED=$(crictl images -o json 2>/dev/null | python3 -c "import sys,json; imgs=json.load(sys.stdin).get('images',[]); print('yes' if any('gpu-dev-image' in str(i.get('repoTags',[])) for i in imgs) else 'no')" 2>/dev/null || echo "no")
  echo "PRE-PULL: Baked AMI image cached=$CACHED"
  if [ "$CACHED" = "yes" ]; then
    echo "PRE-PULL: Using cached image from baked AMI"
  else
    echo "PRE-PULL: Pulling image fresh..."
    crictl pull "$ECR_IMAGE" 2>&1 || echo "Image pre-pull failed"
  fi
  # Pre-pull init container image (used by every pod for SSH key setup)
  crictl pull docker.io/library/alpine:3.21 2>&1 || echo "Alpine pre-pull failed"
  # Pre-pull GPU Operator images (saves ~10 min waiting for DaemonSet pod startup)
  for IMG in \
    nvcr.io/nvidia/k8s/container-toolkit:v1.17.8-ubuntu20.04 \
    nvcr.io/nvidia/k8s-device-plugin:v0.17.4 \
    nvcr.io/nvidia/cloud-native/dcgm:4.3.1-1-ubuntu22.04 \
    nvcr.io/nvidia/k8s/dcgm-exporter:4.3.1-4.4.0-ubuntu22.04 \
    nvcr.io/nvidia/cloud-native/k8s-mig-manager:v0.12.3-ubuntu20.04; do
    crictl pull "$IMG" 2>&1 || echo "GPU Operator image pull failed: $IMG"
  done
) &
echo "*/30 * * * * ECR_LOGIN=\$(aws ecr get-login-password --region ${region}) && echo \$ECR_LOGIN | crictl pull --creds AWS:\$ECR_LOGIN $ECR_IMAGE 2>&1 | logger -t gpu-dev-image-pull" | crontab -

echo "Amazon Linux 2023 EKS GPU node setup completed"