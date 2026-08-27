#!/bin/bash
set -e

# Build and push PyTorch GPU Dev container image with EFA support
# This script builds the Docker image with AWS EFA (Elastic Fabric Adapter) support
# for high-performance multi-node GPU communication.

# Configuration
AWS_REGION="${AWS_REGION:-us-east-2}"
AWS_ACCOUNT_ID="${AWS_ACCOUNT_ID:-308535385114}"
ECR_REPO="${ECR_REPO:-pytorch-gpu-dev-gpu-dev-image}"
IMAGE_TAG="${IMAGE_TAG:-latest}"
IMAGE_URI="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${ECR_REPO}:${IMAGE_TAG}"

echo "=================================================="
echo "Building PyTorch GPU Dev Image with EFA Support"
echo "=================================================="
echo "AWS Account: ${AWS_ACCOUNT_ID}"
echo "Region: ${AWS_REGION}"
echo "ECR Repo: ${ECR_REPO}"
echo "Image Tag: ${IMAGE_TAG}"
echo "Full URI: ${IMAGE_URI}"
echo ""

# Check if Docker is running
if ! docker info > /dev/null 2>&1; then
    echo "Error: Docker is not running. Please start Docker and try again."
    exit 1
fi

# Get script directory
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "${SCRIPT_DIR}"

echo "Building Docker image..."
docker build \
    --platform linux/amd64 \
    -t "${IMAGE_URI}" \
    -f Dockerfile \
    . || { echo "Docker build failed"; exit 1; }

echo ""
echo "Build completed successfully!"
echo ""
echo "Image: ${IMAGE_URI}"
echo ""

# Verify EFA components are installed
echo "Verifying EFA components in the image..."
docker run --rm "${IMAGE_URI}" bash -c '
    echo "Checking libfabric..."
    if [ -f "/opt/amazon/efa/lib64/libfabric.so" ]; then
        echo "  ✓ libfabric found"
    else
        echo "  ✗ libfabric NOT found"
        exit 1
    fi

    echo "Checking AWS OFI-NCCL plugin..."
    if [ -f "/opt/amazon/ofi-nccl/lib/libnccl-net.so" ]; then
        echo "  ✓ OFI-NCCL plugin found"
    else
        echo "  ✗ OFI-NCCL plugin NOT found"
        exit 1
    fi

    echo "Checking OpenMPI..."
    if [ -f "/opt/amazon/openmpi/bin/mpirun" ]; then
        echo "  ✓ OpenMPI found"
    else
        echo "  ✗ OpenMPI NOT found"
        exit 1
    fi

    echo "Checking fi_info..."
    if [ -f "/opt/amazon/efa/bin/fi_info" ]; then
        echo "  ✓ fi_info found"
    else
        echo "  ✗ fi_info NOT found"
        exit 1
    fi

    echo ""
    echo "All EFA components verified!"
'

echo ""
echo "=================================================="
echo "Next Steps:"
echo "=================================================="
echo ""
echo "1. Log in to ECR:"
echo "   aws ecr get-login-password --region ${AWS_REGION} | docker login --username AWS --password-stdin ${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
echo ""
echo "2. Push the image:"
echo "   docker push ${IMAGE_URI}"
echo ""
echo "3. Test EFA in a multi-node reservation:"
echo "   - Create a 2-node reservation: gpu-dev reserve --hours 2 --gpu-count 8 --multi-node 2"
echo "   - SSH into the pods and run NCCL tests:"
echo "     mpirun --hostfile ~/hostfile -np 16 -N 8 \\"
echo "       --mca pml ^cm --mca btl tcp,self \\"
echo "       --mca btl_tcp_if_include enp71s0 \\"
echo "       --mca oob_tcp_if_include enp71s0 \\"
echo "       --mca plm_rsh_args '-p 2222' \\"
echo "       -x PATH -x LD_LIBRARY_PATH \\"
echo "       -x FI_PROVIDER=efa -x FI_EFA_USE_DEVICE_RDMA=1 \\"
echo "       -x NCCL_NET_GDR_LEVEL=SYS -x NCCL_ALGO=ring,tree \\"
echo "       -x NCCL_SOCKET_IFNAME=enp71s0 -x NCCL_DEBUG=INFO \\"
echo "       -x NCCL_IB_HCA=^mlx -x NCCL_CROSS_NIC=0 \\"
echo "       -x NCCL_IB_PCI_RELAXED_ORDERING=1 \\"
echo "       /opt/nccl-tests/build/all_reduce_perf -b 8 -e 1G -f 2 -g 1"
echo ""
echo "Expected performance with EFA + GPUDirect RDMA:"
echo "  - Inter-node bus bandwidth: 300-330 GB/s at 1GB messages"
echo "  - Average bus bandwidth: ~65 GB/s across all sizes"
echo ""
