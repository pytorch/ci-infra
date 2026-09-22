#!/usr/bin/env bash
# Multinode test for `gpu-dev submit`: runs on rank 0 only and orchestrates the
# whole cluster via mpirun (uses passwordless ssh + the headless service DNS we
# already set up). Verifies env vars, peer connectivity, and an actual NCCL
# all_reduce across all nodes.
set -euo pipefail
cd "$(dirname "$0")"

echo "=== rank 0 host: $(hostname) at $(date -u) ==="

echo "=== multinode env ==="
{
  echo "MULTINODE_HOSTS=$MULTINODE_HOSTS"
  echo "MULTINODE_PEER_PODS=$MULTINODE_PEER_PODS"
  echo "MULTINODE_RANK=$MULTINODE_RANK"
  echo "MULTINODE_SIZE=$MULTINODE_SIZE"
  echo "MASTER_ADDR=$MASTER_ADDR"
  echo "MASTER_PORT=$MASTER_PORT"
  echo "MULTINODE_IPS=${MULTINODE_IPS:-(not set)}"
} | tee multinode-env.txt

if [[ -z "${MULTINODE_HOSTS:-}" ]]; then
    echo "ERROR: MULTINODE_HOSTS empty — submit with --gpus >= 16 on h100" >&2
    exit 2
fi

# Resolve IPs even if the bashrc helper didn't run (defensive)
IPS=""
for h in $(echo "$MULTINODE_HOSTS" | tr ',' ' '); do
    ip=$(getent hosts "$h" | awk '{print $1}' | head -1)
    [[ -n "$ip" ]] && IPS="${IPS:+$IPS,}$ip"
done
echo "Resolved IPS=$IPS" | tee resolved-ips.txt

echo "=== peer ssh check (port 2222 inside cluster) ==="
peer_host=$(echo "$MULTINODE_HOSTS" | cut -d, -f2)
ssh -o StrictHostKeyChecking=no -p 2222 "$peer_host" 'hostname; nvidia-smi -L | wc -l' \
    | tee peer-ssh.txt

GPUS_PER_NODE=$(nvidia-smi -L | wc -l)
echo "GPUS_PER_NODE=$GPUS_PER_NODE" | tee gpus-per-node.txt

# Build --host arg: ip1:N,ip2:N,...
HOST_ARG=$(echo "$IPS" | awk -v g="$GPUS_PER_NODE" -F, '{out=""; for(i=1;i<=NF;i++){out=out ($i ":" g) (i<NF?",":"")}; print out}')
echo "HOST_ARG=$HOST_ARG"

echo "=== NCCL all_reduce_perf via mpirun ==="
# Note: -g 1 = 1 GPU per process, -n 20 iterations. Sweep 1M..1G in factor-of-2 steps.
mpirun --host "$HOST_ARG" \
    --mca plm_rsh_args "-p 2222 -o StrictHostKeyChecking=no" \
    -x PATH -x LD_LIBRARY_PATH \
    -x FI_PROVIDER -x FI_EFA_USE_DEVICE_RDMA \
    -x NCCL_NET_GDR_LEVEL -x NCCL_ALGO \
    -x NCCL_SOCKET_IFNAME -x NCCL_DEBUG -x NCCL_IB_HCA \
    /opt/nccl-tests/build/all_reduce_perf -b 1M -e 1G -f 2 -g 1 -n 20 \
    2>&1 | tee nccl-all_reduce.log

echo "=== summary ==="
{
    echo "rank=$MULTINODE_RANK size=$MULTINODE_SIZE"
    echo "host_arg=$HOST_ARG"
    echo "completed at $(date -u)"
} | tee summary.txt

echo "DONE"
