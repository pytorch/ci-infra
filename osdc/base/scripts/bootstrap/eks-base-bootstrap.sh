#!/usr/bin/env bash
# EKS Base Infrastructure Node Bootstrap Script (AL2023)
# This script runs AFTER the EKS bootstrap process
# It is called from the Terraform launch template

set -euo pipefail

# The EKS bootstrap script must be called FIRST by the launch template
# This script contains post-bootstrap configuration only

echo "Starting base infrastructure node post-bootstrap at $(date)"
echo "Amazon Linux 2023 detected"

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

			[host."http://localhost:$HARBOR_PORT/v2/$project"]
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
