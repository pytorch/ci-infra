#!/usr/bin/env bash
# Build an EKS GPU AMI on a newer NVIDIA driver branch than AWS publishes, from
# upstream's own AMI build rather than by mutating a finished image.
#
#   just build-gpu-driver-ami <cluster>
#
# Upstream is cloned at a pinned tag, two patches are applied, and their `make`
# runs unchanged. The patches are the whole delta and are meant to be readable
# as an upstream contribution — see patches/ and README.md.
#
# This does NOT need a mirror of AWS's GRID runfile bucket. The first patch
# stops resolving the driver version against the GRID runfile and the
# proprietary kmod on branches where NVIDIA no longer ships them, so a
# compute-only image builds from the open kmod alone.
set -euo pipefail

: "${CLUSTER:?CLUSTER must be set}"
: "${AWS_DEFAULT_REGION:=}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PATCH_DIR="${SCRIPT_DIR}/patches"

# Pinned together on purpose: the patches carry upstream context lines, so the
# tag they were generated against is part of their contract. `git apply` fails
# loudly on drift, which is the point — bumping the tag means re-reviewing them.
UPSTREAM_REPO="${UPSTREAM_REPO:-https://github.com/awslabs/amazon-eks-ami.git}"
UPSTREAM_REF="${UPSTREAM_REF:-v20260903}"

# R615 is the branch CUDA 13.4 asks for. Major only: upstream resolves the full
# version from the NVIDIA repo, and pinning the full version here would mean
# editing two places every time the branch gets a point release.
NVIDIA_DRIVER_MAJOR_VERSION="${NVIDIA_DRIVER_MAJOR_VERSION:-615}"

# Karpenter selects on this tag (see README for the def snippet). Applied after
# the build rather than patched into upstream's tag block — one less patch, and
# the selector stays owned by this repo.
AMI_TAG_VALUE="${AMI_TAG_VALUE:-gpu-nvidia-${NVIDIA_DRIVER_MAJOR_VERSION}}"

if [[ "${NVIDIA_DRIVER_MAJOR_VERSION}" -lt 615 ]]; then
  echo >&2 "ERROR: below 615 the stock AMI already works; this build only exists for the open-kmod-only branches"
  exit 1
fi

REGION=$(uv run "${SCRIPT_DIR}/../../../scripts/cluster-config.py" "$CLUSTER" region)
CNAME=$(uv run "${SCRIPT_DIR}/../../../scripts/cluster-config.py" "$CLUSTER" cluster_name)
K8S_VERSION=$(uv run "${SCRIPT_DIR}/../../../scripts/cluster-config.py" "$CLUSTER" eks_version)

AMI_NAME="osdc-gpu-nvidia${NVIDIA_DRIVER_MAJOR_VERSION}-k8s${K8S_VERSION}-$(date -u '+%Y%m%d%H%M%S')"

echo "Building ${AMI_NAME}"
echo "  cluster     ${CLUSTER} (${CNAME})"
echo "  region      ${REGION}"
echo "  k8s         ${K8S_VERSION}"
echo "  driver      R${NVIDIA_DRIVER_MAJOR_VERSION}"
echo "  upstream    ${UPSTREAM_REF}"

################################################################################
### Build network ##############################################################
################################################################################
# Upstream takes a subnet id, not a filter, so resolve the cluster's public
# subnet the same way the agent-sandbox build's subnet_filter does. Public
# because the build pulls driver RPMs from developer.download.nvidia.com.
SUBNET_ID=$(aws ec2 describe-subnets --region "$REGION" \
  --filters "Name=tag:Name,Values=${CNAME}*-vpc-public-*" \
  --query 'sort_by(Subnets,&AvailableIpAddressCount)[-1].SubnetId' --output text)

if [[ -z "$SUBNET_ID" || "$SUBNET_ID" == "None" ]]; then
  echo >&2 "ERROR: no public subnet found for ${CNAME} in ${REGION}"
  exit 1
fi
echo "  subnet      ${SUBNET_ID}"

################################################################################
### Upstream checkout + patches ################################################
################################################################################
WORK_DIR=$(mktemp -d)
trap 'rm -rf "${WORK_DIR}"' EXIT

echo "Cloning ${UPSTREAM_REPO} at ${UPSTREAM_REF}..."
git clone --quiet --depth 1 --branch "${UPSTREAM_REF}" "${UPSTREAM_REPO}" "${WORK_DIR}/amazon-eks-ami"

for patch in "${PATCH_DIR}"/*.patch; do
  echo "Applying $(basename "${patch}")"
  # No --3way and no fuzz: a patch that no longer applies cleanly means the
  # upstream logic moved under us, and that needs a human, not a merge.
  git -C "${WORK_DIR}/amazon-eks-ami" apply --verbose "${patch}"
done

################################################################################
### Build ######################################################################
################################################################################
# Every Packer variable is settable as a make variable; see upstream's Makefile.
# enable_efa stays at upstream's default (true) — p5 and p6-b200 need it.
make -C "${WORK_DIR}/amazon-eks-ami" build \
  os_distro=al2023 \
  k8s="${K8S_VERSION}" \
  arch=x86_64 \
  aws_region="${REGION}" \
  enable_accelerator=nvidia \
  nvidia_driver_major_version="${NVIDIA_DRIVER_MAJOR_VERSION}" \
  ami_name="${AMI_NAME}" \
  ami_description="EKS AL2023 GPU + NVIDIA R${NVIDIA_DRIVER_MAJOR_VERSION} for the OSDC compute GPU fleets" \
  subnet_id="${SUBNET_ID}" \
  associate_public_ip_address=true \
  launch_block_device_mappings_volume_size=100

################################################################################
### Tag for Karpenter ##########################################################
################################################################################
MANIFEST="${WORK_DIR}/amazon-eks-ami/${AMI_NAME}-manifest.json"
if [[ ! -f "${MANIFEST}" ]]; then
  echo >&2 "ERROR: no manifest at ${MANIFEST}; build did not produce an AMI"
  exit 1
fi

AMI_ID=$(jq -r --arg region "$REGION" \
  '.builds[-1].artifact_id | split(",")[] | select(startswith($region + ":")) | split(":")[1]' \
  "${MANIFEST}")

if [[ -z "${AMI_ID}" ]]; then
  echo >&2 "ERROR: could not find an AMI id for ${REGION} in ${MANIFEST}"
  exit 1
fi

# Until this runs the AMI is invisible to Karpenter, which is the safe order:
# a half-built image never gets selected.
aws ec2 create-tags --region "$REGION" --resources "${AMI_ID}" --tags \
  "Key=osdc.io/ami,Value=${AMI_TAG_VALUE}" \
  "Key=osdc.io/module,Value=nodepools" \
  "Key=NvidiaDriver,Value=R${NVIDIA_DRIVER_MAJOR_VERSION}" \
  "Key=UpstreamRef,Value=${UPSTREAM_REF}" \
  "Key=K8sVersion,Value=${K8S_VERSION}" \
  "Key=Cluster,Value=${CNAME}" \
  "Key=Project,Value=ciforge"

echo ""
echo "Built ${AMI_ID} (${AMI_NAME}) in ${REGION}, tagged osdc.io/ami=${AMI_TAG_VALUE}"
echo "Unused until a nodepool def opts in with:"
echo "    ami_selector_tags:"
echo "      osdc.io/ami: ${AMI_TAG_VALUE}"
