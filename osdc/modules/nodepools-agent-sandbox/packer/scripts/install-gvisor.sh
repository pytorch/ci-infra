#!/usr/bin/env bash
# Bake gVisor (runsc) into the ai-sandbox AMI. Runs once at IMAGE BUILD time
# under packer — never at node boot.
#
# Two things this deliberately does NOT do, both of which the previous userData
# version did:
#   * fetch anything at node boot — the binaries are in the image
#   * touch /etc/containerd/config.toml or restart containerd — nodeadm
#     regenerates that file at every boot, and restarting containerd after
#     kubelet has started races whatever is already running on the node
#
# Instead the runtime handler is declared as a nodeadm NodeConfig drop-in.
# nodeadm merges /etc/eks/nodeadm.d/* with the user-data config and writes
# /etc/containerd/config.toml BEFORE starting containerd, so runsc is registered
# at containerd's first start.
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

# EKS AL2023 ships containerd v2.x, whose config is `version = 3` and whose CRI
# plugin key is `io.containerd.cri.v1.runtime` (NOT the containerd-1.x
# `io.containerd.grpc.v1.cri` — a v1-style stanza is silently ignored and runsc
# never registers). This stanza is additive; the default runc runtime is
# untouched, so anything without runtimeClassName still runs under runc.
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

# Fail the build rather than ship an image whose runsc can't run. `runsc do`
# exercises the full sandbox (platform detection included) in one shot.
runsc --version
# "do" is a runsc subcommand, quoted so shellcheck doesn't read it as the shell keyword.
runsc --network=none "do" /bin/true

echo "gVisor ${GVISOR_RELEASE} baked: $(runsc --version | head -1)"
