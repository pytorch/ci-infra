#!/bin/bash
set -euo pipefail

# ---- Registry mirror configuration (Harbor pull-through cache) ----
# Resolve this node's primary IPv6 address from IMDS, then write a
# `<NODE_IPV6> harbor` entry to /etc/hosts so containerd can address
# Harbor as `harbor:30002`. Kube-proxy in IPv6-only mode opens NodePort
# listeners on `[::]:30002` but does NOT route traffic destined for `::1`
# to them. The node's primary IPv6 IS routable.
echo "Post-bootstrap: Resolving node IPv6 from IMDS..."
NODE_IPV6=""
IMDS_BASE="http://169.254.169.254"
IMDS_TOKEN_TTL_SECONDS=21600
IMDS_RETRIES=5
IMDS_RETRY_SLEEP=2

# IMDS can return transient failures during early boot before the network stack is fully ready.
fetch_imds() {
  local method=$1
  local path=$2
  local extra_header=$3
  local attempt
  for attempt in $(seq 1 "$IMDS_RETRIES"); do
    if curl -fsS --connect-timeout 2 --max-time 5 -X "$method" -H "$extra_header" "${IMDS_BASE}${path}"; then
      return 0
    fi
    echo "[b200-node-setup] IMDS ${method} ${path} attempt ${attempt}/${IMDS_RETRIES} failed" >&2
    sleep "$IMDS_RETRY_SLEEP"
  done
  return 1
}

TOKEN=$(fetch_imds PUT /latest/api/token "X-aws-ec2-metadata-token-ttl-seconds: ${IMDS_TOKEN_TTL_SECONDS}" || true)
if [[ -n "$TOKEN" ]]; then
  NODE_IPV6=$(fetch_imds GET /latest/meta-data/ipv6 "X-aws-ec2-metadata-token: ${TOKEN}" || true)
fi

HOSTS_MARKER="# managed-by: osdc-harbor-mirror"
if [[ -z "$NODE_IPV6" ]]; then
  echo "WARNING: [b200-node-setup] Could not resolve node IPv6 from IMDS; /etc/hosts not updated" >&2
elif [[ ! "$NODE_IPV6" =~ ^[0-9a-fA-F:]+$ ]]; then
  echo "WARNING: [b200-node-setup] IMDS returned non-IPv6 value '$NODE_IPV6'; /etc/hosts not updated" >&2
else
  sed -i "/${HOSTS_MARKER}/d" /etc/hosts
  echo "${NODE_IPV6} harbor ${HOSTS_MARKER}" >>/etc/hosts
  echo "Wrote /etc/hosts entry: ${NODE_IPV6} harbor"
fi

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

[host."http://harbor:$HARBOR_PORT/v2/$project"]
  capabilities = ["pull", "resolve"]
  skip_verify = true
  override_path = true

[host."$upstream"]
  capabilities = ["pull", "resolve"]
MIRRORS
done
echo "Registry mirrors configured for 6 registries"

# ---- CPU performance tuning ----
echo "Post-bootstrap: Configuring performance settings for p6-b200.48xlarge..."

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

# ---- Fabric Manager / IMEX: do nothing here ----
# Background for future maintainers:
#  * The AMI (amazon-eks-node-al2023-x86_64-nvidia-*) already installs and
#    enables nvidia-fabricmanager and nvidia-persistenced. systemd brings FM
#    up later in boot, after the GPU-resident "local FM" instances have
#    initialized.
#  * Starting FM from cloud-init (user_data) races with that GPU-side init:
#    the host FM finishes NVSwitch routing in seconds but then waits up to
#    GFM Wait Timeout (30 s) for the GPU local-FM instances to register over
#    NVLink Inband. During cloud-init they are usually not ready yet, so FM
#    exits with "config error type 8 / not all local fabric manager
#    instances finished their configuration", taking the whole node out.
#  * IMEX support on this AMI is compiled into nvidia.ko (char device class
#    nvidia-caps-imex-channels, major 242). There is no separate
#    nvidia-caps-imex-channels.ko; `modprobe nvidia-caps-imex-channels`
#    will always fail.
#  * Single-node NVLS multicast on p6-b200 does not require an IMEX channel
#    device - NCCL uses POSIX FD handles intranode. IMEX channels are only
#    needed for multi-node NVLink (MNNVL / GB200 UltraServers).
# If a fresh provision ever shows FM failing on the normal boot path (not
# cloud-init), prefer a systemd drop-in with After=nvidia-persistenced.service
# and Restart=on-failure over taking control of FM here.

echo "Performance configuration complete for p6-b200.48xlarge"
