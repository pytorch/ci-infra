#!/usr/bin/env bash
# EKS Base Infrastructure Node Bootstrap Script (AL2023)
# This script runs AFTER the EKS bootstrap process
# It is called from the Terraform launch template

set -euo pipefail

# The EKS bootstrap script must be called FIRST by the launch template
# This script contains post-bootstrap configuration only

echo "Starting base infrastructure node post-bootstrap at $(date)"
echo "Amazon Linux 2023 detected"

# Resolve this node's primary IPv6 address from IMDS, then write a
# `<NODE_IPV6> harbor` entry to /etc/hosts so containerd can address
# Harbor as `harbor:30002` regardless of stack family. Kube-proxy in
# IPv6-only mode opens NodePort listeners on `[::]:30002` but does NOT
# route traffic destined for `::1` to them, so the IPv6 loopback form
# does not work. Each node's primary IPv6 IS routable and reaches
# kube-proxy.
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
    echo "[eks-base-bootstrap] IMDS ${method} ${path} attempt ${attempt}/${IMDS_RETRIES} failed" >&2
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
  echo "WARNING: [eks-base-bootstrap] Could not resolve node IPv6 from IMDS; /etc/hosts not updated" >&2
elif [[ ! "$NODE_IPV6" =~ ^[0-9a-fA-F:]+$ ]]; then
  echo "WARNING: [eks-base-bootstrap] IMDS returned non-IPv6 value '$NODE_IPV6'; /etc/hosts not updated" >&2
else
  # Idempotent rewrite: drop any prior entry, append fresh one.
  sed -i "/${HOSTS_MARKER}/d" /etc/hosts
  echo "${NODE_IPV6} harbor ${HOSTS_MARKER}" >>/etc/hosts
  echo "Wrote /etc/hosts entry: ${NODE_IPV6} harbor"
fi

# AL2023 uses containerd by default (not Docker)
# Configure containerd if needed
if systemctl is-active --quiet containerd; then
  echo "Containerd is running"

  # Configure registry mirrors for Harbor pull-through cache
  echo "Configuring registry mirrors for Harbor pull-through cache..."
  HARBOR_PORT=30002

  for registry_project in \
    "docker.io dockerhub-cache https://docker.io" \
    "ghcr.io ghcr-cache https://ghcr.io" \
    "public.ecr.aws ecr-public-cache https://public.ecr.aws" \
    "nvcr.io nvcr-cache https://nvcr.io" \
    "registry.k8s.io k8s-cache https://registry.k8s.io" \
    "quay.io quay-cache https://quay.io"; do
    # shellcheck disable=SC2086  # intentional word splitting
    set -- $registry_project
    registry=$1
    project=$2
    upstream=$3
    mkdir -p "/etc/containerd/certs.d/$registry"
    cat >"/etc/containerd/certs.d/$registry/hosts.toml" <<-MIRRORS
			server = "$upstream"

			[host."http://harbor:$HARBOR_PORT/v2/$project"]
			  capabilities = ["pull", "resolve"]
			  skip_verify = true
			  override_path = true

			[host."$upstream"]
			  capabilities = ["pull", "resolve"]
		MIRRORS
  done
  echo "Registry mirrors configured for 6 registries (Harbor port: $HARBOR_PORT)"
fi

# Install useful tools (AL2023 uses dnf)
dnf install -y \
  htop \
  iotop \
  sysstat \
  vim \
  wget \
  curl \
  git \
  ccache

# Configure node for infrastructure workloads
sysctl -w vm.max_map_count=262144
echo "vm.max_map_count=262144" >>/etc/sysctl.conf

# Set up ccache directory
mkdir -p /var/cache/ccache
chmod 777 /var/cache/ccache

echo "Base infrastructure node post-bootstrap completed at $(date)"
echo "Node taint: CriticalAddonsOnly=true:NoSchedule"
echo "This node will only run system components with matching tolerations"
