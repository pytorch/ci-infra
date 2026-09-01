#!/usr/bin/env bash
# Bake gVisor (runsc) into the ai-sandbox AMI. Build time only, never node boot.
#
# ENABLE_NVPROXY=1 additionally turns on nvproxy, which forwards NVIDIA ioctls from the
# sandbox to the host driver — only for the GPU AMI. See docs/agent-sandbox-gpu-gvisor.md
# for what that gives up.
#
# Deliberately does not touch /etc/containerd/config.toml or restart containerd:
# nodeadm rewrites that file every boot, and a restart after kubelet has started
# races whatever is already on the node. The runtime handler is declared as a
# nodeadm drop-in instead, which nodeadm merges before starting containerd.
set -euxo pipefail

: "${GVISOR_RELEASE:?GVISOR_RELEASE must be set (yyyymmdd release to pin)}"

ARCH="$(uname -m)"
URL_BASE="https://storage.googleapis.com/gvisor/releases/release/${GVISOR_RELEASE}/${ARCH}"

install_bin() {
  local name="$1"
  curl -fsSL "${URL_BASE}/${name}" -o "/usr/local/bin/${name}"
  curl -fsSL "${URL_BASE}/${name}.sha512" -o "/tmp/${name}.sha512"
  (cd /usr/local/bin && sha512sum -c "/tmp/${name}.sha512")
  chmod 0755 "/usr/local/bin/${name}"
  rm -f "/tmp/${name}.sha512"
}

install_bin runsc
install_bin containerd-shim-runsc-v1

# The key must be `io.containerd.cri.v1.runtime`: EKS AL2023 ships containerd
# v2.x (`version = 3` config), where a containerd-1.x `io.containerd.grpc.v1.cri`
# stanza is silently ignored and runsc never registers. Additive — runc is
# untouched, so pods without runtimeClassName are unaffected.
install -d -m 0755 /etc/eks/nodeadm.d
if [[ "${ENABLE_NVPROXY:-0}" == "1" ]]; then
  # runsc reads its own settings from this file; containerd only points at it. nvproxy
  # is off by default in runsc, so the GPU AMI is the only image that turns it on.
  cat >/etc/containerd/runsc.toml <<'EOF'
# Baked into the ai-sandbox GPU AMI by modules/nodepools-agent-sandbox/packer.
[runsc_config]
  nvproxy = "true"
EOF
  chmod 0644 /etc/containerd/runsc.toml
  RUNTIME_OPTIONS=$'\n        [plugins.\'io.containerd.cri.v1.runtime\'.containerd.runtimes.runsc.options]\n          TypeUrl = "io.containerd.runsc.v1.options"\n          ConfigPath = "/etc/containerd/runsc.toml"'
else
  RUNTIME_OPTIONS=""
fi

cat >/etc/eks/nodeadm.d/10-gvisor.yaml <<EOF
# Baked into the ai-sandbox AMI by modules/nodepools-agent-sandbox/packer.
# Merged by nodeadm into /etc/containerd/config.toml before containerd starts.
apiVersion: node.eks.aws/v1alpha1
kind: NodeConfig
spec:
  containerd:
    config: |
      [plugins.'io.containerd.cri.v1.runtime'.containerd.runtimes.runsc]
        runtime_type = "io.containerd.runsc.v1"${RUNTIME_OPTIONS}
EOF
chmod 0644 /etc/eks/nodeadm.d/10-gvisor.yaml

# Fail the build rather than ship an image whose runsc can't run.
runsc --version

if [[ "${ENABLE_NVPROXY:-0}" == "1" ]]; then
  # The pinning gate. nvproxy matches ioctl struct layouts per driver version and refuses
  # a version it does not know, so an AMI whose driver is off the list yields a fleet that
  # cannot start a single GPU sandbox. Fail here, where the fix is a one-line AMI pin,
  # rather than on a node at 3am.
  #
  # Ask the packages, not the device. On this AMI the kernel module is DKMS-built at boot,
  # so during a build there is no nvidia.ko to modinfo and no loaded driver for nvidia-smi
  # to reach — it prints "couldn't communicate with the NVIDIA driver" to stdout, which an
  # earlier version of this check happily treated as a version string. The RPMs carry the
  # exact version whether or not a GPU is present, which is also why the builder needs no
  # GPU.
  driver=""
  for pkg in nvidia-kmod-common kmod-nvidia-latest-dkms kmod-nvidia-open-dkms nvidia-driver; do
    if rpm -q --quiet "$pkg"; then
      driver="$(rpm -q --queryformat '%{VERSION}' "$pkg")"
      break
    fi
  done
  # Fallbacks for an image that ships the driver another way, or for running this on a live
  # GPU node.
  if [[ -z "$driver" ]]; then
    driver="$(modinfo -F version nvidia 2>/dev/null || true)"
  fi
  if [[ -z "$driver" ]]; then
    driver="$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -1 || true)"
  fi
  # Shape check, because every probe above can fail by printing prose to stdout rather than
  # by exiting non-zero. Without this the grep below compares an error message against the
  # supported list and reports "driver <sentence> is not supported", which sends you
  # looking for the wrong problem.
  if [[ ! "$driver" =~ ^[0-9]+\.[0-9]+(\.[0-9]+)?$ ]]; then
    echo "ERROR: could not read an NVIDIA driver version from the base AMI (got '${driver}')." >&2
    echo "Is nvidia_ami_release pointing at the nvidia variant? Check by hand with:" >&2
    echo "  rpm -qa | grep -i nvidia" >&2
    exit 1
  fi
  if ! runsc nvproxy list-supported-drivers | grep -qx "$driver"; then
    echo "ERROR: NVIDIA driver ${driver} is not supported by gVisor ${GVISOR_RELEASE}." >&2
    echo "Pin nvidia_ami_release to a release whose driver is one of:" >&2
    runsc nvproxy list-supported-drivers | tr '\n' ' ' >&2
    echo "" >&2
    exit 1
  fi
  echo "nvproxy: driver ${driver} is supported by ${GVISOR_RELEASE}"
fi
# "do" is a runsc subcommand, quoted so shellcheck doesn't read it as the shell keyword.
runsc --network=none "do" /bin/true

echo "gVisor ${GVISOR_RELEASE} baked: $(runsc --version | head -1)"
