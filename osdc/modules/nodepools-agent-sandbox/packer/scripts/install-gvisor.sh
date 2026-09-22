#!/usr/bin/env bash
# Bake gVisor (runsc) into the ai-sandbox AMI. Build time only, never node boot.
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
cat >/etc/eks/nodeadm.d/10-gvisor.yaml <<'EOF'
# Baked into the ai-sandbox AMI by modules/nodepools-agent-sandbox/packer.
# Merged by nodeadm into /etc/containerd/config.toml before containerd starts.
apiVersion: node.eks.aws/v1alpha1
kind: NodeConfig
spec:
  containerd:
    config: |
      [plugins.'io.containerd.cri.v1.runtime'.containerd.runtimes.runsc]
        runtime_type = "io.containerd.runsc.v1"
EOF
chmod 0644 /etc/eks/nodeadm.d/10-gvisor.yaml

# Fail the build rather than ship an image whose runsc can't run.
runsc --version
# "do" is a runsc subcommand, quoted so shellcheck doesn't read it as the shell keyword.
runsc --network=none "do" /bin/true

echo "gVisor ${GVISOR_RELEASE} baked: $(runsc --version | head -1)"
