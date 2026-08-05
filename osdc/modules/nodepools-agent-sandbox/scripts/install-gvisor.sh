#!/usr/bin/env bash
# gVisor (runsc) install for AI-agent sandbox nodes.
#
# Embedded as a text/x-shellscript userData MIME part by the nodepools generator
# (referenced from defs/ai-sandbox.yaml via user_data_script). Runs once at node
# boot, AFTER nodeadm has written the EKS containerd config and started kubelet.
# The node still has full egress at boot (pod-level NetworkPolicy does not apply
# to the host), so fetching the release binaries here is fine.
#
# Best-effort by design: this fleet is dedicated to sandbox pods only. If the
# install fails, only runtimeClassName=gvisor pods fail to start (fail-closed) —
# the rest of the cluster is unaffected. To iterate without gVisor, drop
# runtimeClassName from the agent pod spec and pin it to this fleet manually.
set -euxo pipefail

ARCH="$(uname -m)"
URL_BASE="https://storage.googleapis.com/gvisor/releases/release/latest/${ARCH}"

install_bin() {
  local name="$1"
  curl -fsSL "${URL_BASE}/${name}" -o "/usr/local/bin/${name}"
  curl -fsSL "${URL_BASE}/${name}.sha512" -o "/tmp/${name}.sha512"
  (cd /usr/local/bin && sha512sum -c "/tmp/${name}.sha512")
  chmod 0755 "/usr/local/bin/${name}"
}

install_bin runsc
install_bin containerd-shim-runsc-v1

# Register runsc as a containerd runtime handler. EKS AL2023 ships a single
# /etc/containerd/config.toml; appending a runtime stanza is additive and does
# not disturb the default runc runtime.
CONFIG=/etc/containerd/config.toml
if ! grep -q 'containerd.runtimes.runsc' "${CONFIG}"; then
  cat >>"${CONFIG}" <<'EOF'

[plugins."io.containerd.grpc.v1.cri".containerd.runtimes.runsc]
  runtime_type = "io.containerd.runsc.v1"
EOF
fi

systemctl restart containerd
