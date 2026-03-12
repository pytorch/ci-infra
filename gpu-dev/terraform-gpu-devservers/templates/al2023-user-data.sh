#!/bin/bash

# Simple Amazon Linux 2023 EKS node setup
# Remove any existing NVIDIA drivers and let GPU Operator manage them

set -o xtrace

# Disable the default nodeadm services that try to parse user-data as config
systemctl disable nodeadm-config.service || true
systemctl disable nodeadm-run.service || true
systemctl stop nodeadm-config.service || true
systemctl stop nodeadm-run.service || true

# Install NVIDIA driver 580.82.07 directly on host for CUDA 13 support
# GPU Operator will handle toolkit/device-plugin only

# Configure NVIDIA profiling BEFORE driver installation (driver install auto-loads modules)
# Required for ncu/nsys GPU profiling tools
echo "options nvidia NVreg_RestrictProfilingToAdminUsers=0" > /etc/modprobe.d/nvprof.conf

# Install NVIDIA driver
dnf config-manager --add-repo https://developer.download.nvidia.com/compute/cuda/repos/amzn2023/x86_64/cuda-amzn2023.repo
dnf install -y nvidia-driver nvidia-driver-cuda

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
if [[ "${gpu_type}" == "a100" || "${gpu_type}" == "b200" || "${gpu_type}" == "h200" || "${gpu_type}" == "h100" ]]; then
    echo "Installing fabric manager for multi-GPU system: ${gpu_type}"

    # Install InfiniBand tools - EFA hardware is already present and configured
    echo "Installing InfiniBand diagnostic tools for fabric manager..."
    dnf install -y infiniband-diags

    # Install fabric manager and NVLink Subnet Manager
    dnf install -y nvidia-fabricmanager nvlsm

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
      - --node-labels=NodeType=gpu,GpuType=${gpu_type},nvidia.com/gpu.deploy.driver=false${profiling_dedicated ? ",gpu.monitoring/profiling-dedicated=true,nvidia.com/gpu.deploy.dcgm-exporter=false" : ""}
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

# Pre-pull GPU dev container image and refresh every 30 minutes
# ECR credentials are handled by kubelet's credential provider
ECR_IMAGE="${container_image}"
crictl pull "$ECR_IMAGE" || echo "Initial image pre-pull failed (node may not be ready yet)"
echo "*/30 * * * * ECR_LOGIN=\$(aws ecr get-login-password --region ${region}) && echo \$ECR_LOGIN | crictl pull --creds AWS:\$ECR_LOGIN $ECR_IMAGE 2>&1 | logger -t gpu-dev-image-pull" | crontab -

echo "Amazon Linux 2023 EKS GPU node setup completed"