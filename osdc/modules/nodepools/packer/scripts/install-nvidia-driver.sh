#!/usr/bin/env bash
# Replace the stock NVIDIA driver on the EKS-optimized AL2023 GPU AMI with a
# newer branch. Build time only, never node boot.
#
# The EKS AMI ships driver 580 and there is no knob to change that: upstream's
# own build resolves the version as min(kmod-nvidia-open-dkms, AWS GRID runfile)
# and AWS's GRID bucket stops at 595, so even building the AMI from source can
# not reach 615. See docs/nvidia-driver-615-ami.md for the full analysis.
#
# What this does instead is take the finished GPU AMI and swap the driver in
# place, following the same sequence upstream's install-nvidia-driver.sh uses:
# rebuild the DKMS module, re-archive it under /var/lib/dkms-archive (which is
# what nvidia-kmod-load.service unpacks at boot), then move userspace to match.
#
# Scope: compute GPUs (p4d/p5/p6) only. R615 dropped the proprietary kernel
# module entirely — see the guard installed by patch-kmod-load below.
set -euxo pipefail

: "${NVIDIA_DRIVER_VERSION:?NVIDIA_DRIVER_VERSION must be set (full version, e.g. 615.71.09)}"
: "${NVIDIA_GDRCOPY_VERSION:?NVIDIA_GDRCOPY_VERSION must be set (e.g. 2.5.2)}"

NVIDIA_DRIVER_MAJOR="${NVIDIA_DRIVER_VERSION%%.*}"
DKMS_ARCHIVE_DIR=/var/lib/dkms-archive
KMOD_LOAD=/etc/eks/nvidia-kmod-load.sh
MAJOR_STAMP=/etc/eks/osdc-nvidia-driver-major

################################################################################
### Preflight ##################################################################
################################################################################
# Fail early and loudly if the base image is not what we think it is. Every step
# below edits files the EKS AMI owns, so a base that has moved on is a rebuild
# signal, not something to paper over.
for required in /usr/bin/kmod-util "${KMOD_LOAD}" "${DKMS_ARCHIVE_DIR}"; do
  if [[ ! -e "${required}" ]]; then
    echo >&2 "ERROR: ${required} missing — base AMI is not an EKS AL2023 NVIDIA image"
    exit 1
  fi
done

STOCK_VERSION=$(rpmquery --queryformat '%{VERSION}' nvidia-driver-cuda 2>/dev/null || echo "unknown")
echo "Stock driver: ${STOCK_VERSION} -> target: ${NVIDIA_DRIVER_VERSION}"

if [[ "${STOCK_VERSION}" == "${NVIDIA_DRIVER_VERSION}" ]]; then
  echo >&2 "ERROR: base AMI already ships ${NVIDIA_DRIVER_VERSION} — drop this module and use the stock AMI"
  exit 1
fi

sudo dnf -y install jq

################################################################################
### Point dnf at the target branch #############################################
################################################################################
# The CUDA repo serves kmod-nvidia* through module streams and only the newest
# stream is visible by default; module_hotfixes disables that filtering so an
# exact version can be pinned. Same trick upstream uses.
sudo dnf -y module reset nvidia-driver || true
sudo dnf -y module enable "nvidia-driver:${NVIDIA_DRIVER_MAJOR}-open"

if ! dnf -q repoquery --setopt='*.module_hotfixes=true' \
  "kmod-nvidia-open-dkms-${NVIDIA_DRIVER_VERSION}" | grep -q .; then
  echo >&2 "ERROR: kmod-nvidia-open-dkms-${NVIDIA_DRIVER_VERSION} not found in the configured repos"
  exit 1
fi

# The kernel is versionlocked by the EKS build, so DKMS compiles against exactly
# the kernel this AMI boots. Headers are already present; assert rather than
# install, so a base image that stopped shipping them fails here.
if ! rpmquery "kernel-devel-$(uname -r)" >/dev/null 2>&1; then
  echo >&2 "ERROR: kernel-devel-$(uname -r) missing — cannot build DKMS modules"
  exit 1
fi

################################################################################
### Drop the stock kmod archives ###############################################
################################################################################
# nvidia-kmod-load.service picks a flavor at boot and unpacks it from here. A
# stale 580 tarball left next to 615 userspace is the one failure mode that
# would survive to a running node, so all four go.
#
# nvidia (proprietary) and nvidia-open-grid are removed and never rebuilt:
# R615 ships no proprietary kmod, and AWS publishes no 615 GRID runfile. The
# boot-time guard installed below redirects both selections to nvidia-open.
for flavor in nvidia nvidia-open nvidia-open-grid gdrdrv; do
  sudo rm -rf "${DKMS_ARCHIVE_DIR:?}/${flavor}"
done
sudo rm -rf /usr/src/nvidia-*

################################################################################
### Build and archive the open kmod ############################################
################################################################################
# The RPM and the upstream dkms.conf disagree on the package name (the module
# was renamed nvidia-open -> nvidia in 570.148.08), so the DKMS entry has to be
# re-registered under the name kmod-util and nvidia-kmod-load.sh expect.
# Verbatim from upstream's archive-open-kmods.
sudo dnf -y --setopt='*.module_hotfixes=true' install \
  "kmod-nvidia-open-dkms-${NVIDIA_DRIVER_VERSION}"

sudo dkms remove "nvidia/${NVIDIA_DRIVER_VERSION}" --all
sudo sed -i 's/PACKAGE_NAME="nvidia"/PACKAGE_NAME="nvidia-open"/' \
  "/usr/src/nvidia-${NVIDIA_DRIVER_VERSION}/dkms.conf"
sudo mv "/usr/src/nvidia-${NVIDIA_DRIVER_VERSION}" "/usr/src/nvidia-open-${NVIDIA_DRIVER_VERSION}"
sudo dkms add -m nvidia-open -v "${NVIDIA_DRIVER_VERSION}"
sudo dkms build -m nvidia-open -v "${NVIDIA_DRIVER_VERSION}"
sudo dkms install -m nvidia-open -v "${NVIDIA_DRIVER_VERSION}"
sudo kmod-util archive nvidia-open

# gdrdrv links against the nvidia module's symbols, so it has to be built while
# nvidia-open is still DKMS-installed — same ordering as upstream.
sudo dnf -y install "gdrcopy-kmod-${NVIDIA_GDRCOPY_VERSION}"
sudo kmod-util archive gdrdrv
sudo kmod-util remove gdrdrv
sudo dnf -y remove --all "gdrcopy-kmod-${NVIDIA_GDRCOPY_VERSION}"

sudo kmod-util remove nvidia-open
sudo dnf -y remove --all "kmod-nvidia-open*"
sudo rm -rf /usr/src/nvidia-open-*

################################################################################
### Move userspace to match ####################################################
################################################################################
# Versions are pinned explicitly on the top-level packages; their nvidia-* deps
# are strictly versioned, so dnf pulls the matching set. The assertion at the
# bottom catches anything left behind at the old version.
sudo dnf -y install \
  "libnvidia-fbc-${NVIDIA_DRIVER_VERSION}" \
  "nvidia-driver-${NVIDIA_DRIVER_VERSION}" \
  "nvidia-driver-cuda-${NVIDIA_DRIVER_VERSION}" \
  "nvidia-fabricmanager-${NVIDIA_DRIVER_VERSION}" \
  "nvidia-imex-${NVIDIA_DRIVER_VERSION}" \
  "nvidia-libXNVCtrl-devel-${NVIDIA_DRIVER_VERSION}" \
  "nvidia-modprobe-${NVIDIA_DRIVER_VERSION}" \
  "nvidia-persistenced-${NVIDIA_DRIVER_VERSION}" \
  "nvidia-settings-${NVIDIA_DRIVER_VERSION}" \
  "nvidia-xconfig-${NVIDIA_DRIVER_VERSION}" \
  "xorg-x11-nvidia-${NVIDIA_DRIVER_VERSION}"

# Enabled in the base image already, but nvidia-fabricmanager is reinstalled
# above and RPM does not carry the enablement across.
sudo systemctl enable nvidia-fabricmanager
sudo systemctl enable nvidia-persistenced
sudo systemctl enable nvidia-kmod-load.service

################################################################################
### Supported-device list for the new branch ###################################
################################################################################
# nvidia-kmod-load.sh greps this to decide whether the attached GPUs can use the
# open kmod. Upstream generates it from the .run file at AMI-build time and only
# ships lists up to 595; the nvidia-driver RPM carries the same JSON, so the 615
# list is generated here from the package rather than a 500MB download.
SUPPORTED_GPUS_JSON=/usr/share/doc/nvidia-driver/supported-gpus.json
DEVICE_FILE="/etc/eks/nvidia-open-supported-devices-${NVIDIA_DRIVER_MAJOR}.txt"

if [[ ! -f "${SUPPORTED_GPUS_JSON}" ]]; then
  echo >&2 "ERROR: ${SUPPORTED_GPUS_JSON} not shipped by nvidia-driver-${NVIDIA_DRIVER_VERSION}"
  exit 1
fi

# Same jq filter as upstream hack/generate-nvidia-open-supported-devices.sh.
{
  echo "# Generated at AMI build time from ${SUPPORTED_GPUS_JSON}"
  echo "# (nvidia-driver-${NVIDIA_DRIVER_VERSION}), by osdc modules/nodepools/packer."
  jq -r '.chips[] | select(.features[]? | contains("kernelopen")) | "\(.devid) \(.name)"' \
    "${SUPPORTED_GPUS_JSON}" | sort -u
} | sudo tee "${DEVICE_FILE}" >/dev/null

# Sanity-check the GPUs this AMI actually targets: A100 (0x20B2), H100 (0x2330),
# B200 (0x2901). If the open kmod ever stops covering one of them, the node
# would silently fall through to a flavor that no longer exists.
for devid in 0x20B2 0x2330 0x2901; do
  if ! grep -qi "^${devid} " "${DEVICE_FILE}"; then
    echo >&2 "ERROR: ${devid} not open-kmod supported in ${NVIDIA_DRIVER_VERSION}"
    exit 1
  fi
done

################################################################################
### Patch the boot-time kmod selector ##########################################
################################################################################
# Two upstream assumptions break on 615, both of which would leave a node with
# no driver loaded at all:
#
#   1. The major version is probed with `rpmquery kmod-nvidia-latest-dkms`.
#      That package is the proprietary kmod and does not exist at 615, so the
#      probe fails, devices-support-open returns 1, and selection falls through
#      to the proprietary flavor.
#   2. g4dn/g5/g5g are hardcoded to the proprietary flavor for a GSP workaround,
#      and the GRID path selects nvidia-open-grid. Neither archive exists here.
#
# Both patches are anchored on exact upstream text and the build fails if an
# anchor is missing — a base-AMI refresh that rewrites this script must be
# reviewed, not silently shipped.
echo "${NVIDIA_DRIVER_MAJOR}" | sudo tee "${MAJOR_STAMP}" >/dev/null

VERSION_PROBE="  KMOD_MAJOR_VERSION=\$(rpmquery kmod-nvidia-latest-dkms --queryformat '%{VERSION}' | cut -d. -f1)"
if ! grep -qF "${VERSION_PROBE}" "${KMOD_LOAD}"; then
  echo >&2 "ERROR: version-probe anchor not found in ${KMOD_LOAD}; upstream changed, re-review this patch"
  exit 1
fi
sudo python3 - "${KMOD_LOAD}" "${VERSION_PROBE}" "${MAJOR_STAMP}" <<'PY'
import sys

path, anchor, stamp = sys.argv[1], sys.argv[2], sys.argv[3]
replacement = (
    "  # Patched by osdc modules/nodepools/packer: the stock probe reads the\n"
    "  # proprietary kmod's version, which this AMI does not ship.\n"
    f"  KMOD_MAJOR_VERSION=$(cat {stamp})"
)
with open(path) as f:
    content = f.read()
with open(path, "w") as f:
    f.write(content.replace(anchor, replacement, 1))
PY

# shellcheck disable=SC2016  # literal upstream text to match, not an expansion
LOAD_ANCHOR='kmod-util load "${MODULE_NAME}"'
if ! grep -qF "${LOAD_ANCHOR}" "${KMOD_LOAD}"; then
  echo >&2 "ERROR: kmod-load anchor not found in ${KMOD_LOAD}; upstream changed, re-review this patch"
  exit 1
fi
sudo python3 - "${KMOD_LOAD}" "${LOAD_ANCHOR}" <<'PY'
import sys

path, anchor = sys.argv[1], sys.argv[2]
guard = """# Patched by osdc modules/nodepools/packer. This AMI archives only the open
# kmod: 615 ships no proprietary module and AWS publishes no 615 GRID runfile.
# Redirect rather than fail, so a GPU outside this AMI's intended scope still
# boots with a driver instead of none.
if [ ! -d "/var/lib/dkms-archive/${MODULE_NAME}" ]; then
  echo "No ${MODULE_NAME} archive on this AMI; falling back to nvidia-open"
  MODULE_NAME="nvidia-open"
  # NVreg_EnableGpuFirmware=0 is a proprietary-only workaround and the open
  # kmod requires GSP firmware, so it has to go with the flavor that set it.
  rm -f /etc/modprobe.d/nvidia-disable-gsp.conf
fi

"""
with open(path) as f:
    content = f.read()
with open(path, "w") as f:
    f.write(content.replace(anchor, guard + anchor, 1))
PY

sudo bash -n "${KMOD_LOAD}"

################################################################################
### Verify ####################################################################
################################################################################
# No GPU on the build instance, so nvidia-smi proves nothing. Check the things
# that are actually decided at build time.
shopt -s nullglob

test ! -e "${DKMS_ARCHIVE_DIR}/nvidia"
test ! -e "${DKMS_ARCHIVE_DIR}/nvidia-open-grid"

gdrdrv_archives=("${DKMS_ARCHIVE_DIR}/gdrdrv/"*.tar.gz)
if [[ ${#gdrdrv_archives[@]} -eq 0 ]]; then
  echo >&2 "ERROR: no gdrdrv archive was produced"
  exit 1
fi

# The archive tarball name carries the version DKMS built; this is the check
# that the node will actually load the target driver and not something stale.
open_archives=("${DKMS_ARCHIVE_DIR}/nvidia-open/"*"${NVIDIA_DRIVER_VERSION}"*.tar.gz)
if [[ ${#open_archives[@]} -eq 0 ]]; then
  echo >&2 "ERROR: no archived nvidia-open kmod at ${NVIDIA_DRIVER_VERSION}. Present:"
  ls >&2 -la "${DKMS_ARCHIVE_DIR}/nvidia-open/"
  exit 1
fi

# Anything nvidia-ish left at the old version means dnf resolved a dependency
# we did not pin. nvidia-container-* is excluded: it versions independently of
# the driver and is intentionally left alone.
STALE=$(rpmquery --queryformat '%{NAME} %{VERSION}\n' 'nvidia-*' 'libnvidia-*' 'xorg-x11-nvidia*' \
  | grep -v '^nvidia-container' \
  | grep -v " ${NVIDIA_DRIVER_VERSION}$" || true)
if [[ -n "${STALE}" ]]; then
  echo >&2 "ERROR: packages left at a non-target version:"
  echo >&2 "${STALE}"
  exit 1
fi

sudo dnf clean all

echo "NVIDIA ${NVIDIA_DRIVER_VERSION} baked (was ${STOCK_VERSION}); open kmod only."
