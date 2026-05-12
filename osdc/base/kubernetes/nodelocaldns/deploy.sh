#!/usr/bin/env bash
set -euo pipefail
#
# Deploy NodeLocal DNSCache (NLD) as a per-node DaemonSet.
# Called from the justfile's deploy-base recipe.
#
# Args: $1=cluster-id
#
# NLD requires the kube-dns ClusterIP at config-render time and the
# kube-dns-upstream Service to exist BEFORE the DaemonSet pods are
# scheduled (the binary reads KUBE_DNS_UPSTREAM_SERVICE_HOST, which
# kubelet only injects for Services existing at pod-create time).

# shellcheck disable=SC2034  # CLUSTER is part of the deploy.sh interface
CLUSTER="$1"
if [[ -z "$CLUSTER" ]]; then
  echo "ERROR: cluster-id arg is required" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OSDC_UPSTREAM="${OSDC_UPSTREAM:-$(cd "$SCRIPT_DIR/../../.." && pwd)}"
# shellcheck source=/dev/null
source "$OSDC_UPSTREAM/scripts/mise-activate.sh"
# shellcheck source=/dev/null
source "$OSDC_UPSTREAM/scripts/kubectl-apply.sh"

# --- Cleanup trap ---
WORK_DIR=""
cleanup() {
  [[ -n "$WORK_DIR" ]] && rm -rf "$WORK_DIR" 2>/dev/null || true
}
trap cleanup EXIT

# --- Step 1: Precondition — verify kube-dns-upstream Service is ours (or absent) ---
echo "→ NodeLocal DNSCache: checking kube-dns-upstream Service precondition..."
UPSTREAM_FIRST_DEPLOY=true
EXISTING_SELECTOR=""
if kubectl get svc kube-dns-upstream -n kube-system &>/dev/null; then
  UPSTREAM_FIRST_DEPLOY=false
  EXISTING_SELECTOR=$(kubectl get svc kube-dns-upstream -n kube-system \
    -o jsonpath='{.spec.selector.k8s-app}' 2>/dev/null)
  if [[ "$EXISTING_SELECTOR" != "kube-dns" ]]; then
    echo "ERROR: kube-dns-upstream Service exists with selector k8s-app=${EXISTING_SELECTOR:-<empty>}." >&2
    echo "       Expected selector k8s-app=kube-dns. Refusing to overwrite." >&2
    exit 1
  fi
  echo "  kube-dns-upstream already present with correct selector — no rollout restart needed."
else
  echo "  kube-dns-upstream not present — will create (rollout restart will follow)."
fi

# --- Step 2: Resolve kube-dns ClusterIP ---
echo "→ NodeLocal DNSCache: resolving kube-dns ClusterIP..."
KUBE_DNS_CLUSTER_IP=$(kubectl get svc kube-dns -n kube-system -o jsonpath='{.spec.clusterIP}' 2>/dev/null)
if [[ -z "$KUBE_DNS_CLUSTER_IP" ]]; then
  echo "ERROR: failed to resolve kube-dns ClusterIP (empty result)." >&2
  exit 1
fi
if [[ ! "${KUBE_DNS_CLUSTER_IP}" =~ ^[0-9a-fA-F:.]+$ ]]; then
  echo "ERROR: KUBE_DNS_CLUSTER_IP value is not a valid IPv4/IPv6 address: ${KUBE_DNS_CLUSTER_IP}" >&2
  exit 1
fi
echo "  kube-dns ClusterIP: $KUBE_DNS_CLUSTER_IP"

# --- Step 3: Render manifests via kustomize, then sed-substitute placeholders ---
echo "→ NodeLocal DNSCache: rendering manifests..."
WORK_DIR=$(mktemp -d)
RENDERED="$WORK_DIR/all.yaml"
SERVICES_FILE="$WORK_DIR/services.yaml"
REST_FILE="$WORK_DIR/rest.yaml"

kubectl kustomize "$SCRIPT_DIR" >"$RENDERED"

# .bak suffix for macOS/BSD sed compatibility, then drop the backup.
sed -i.bak "s|__KUBE_DNS_CLUSTER_IP__|${KUBE_DNS_CLUSTER_IP}|g" "$RENDERED"
rm -f "${RENDERED}.bak"

# --- Step 4: Split rendered output — Services first, everything else after ---
# Services must exist before DaemonSet pods are scheduled so kubelet injects
# KUBE_DNS_UPSTREAM_SERVICE_HOST/PORT into the node-cache container env.
uv run --with pyyaml python3 - "$RENDERED" "$SERVICES_FILE" "$REST_FILE" <<'PY'
import sys
import yaml

src, services_path, rest_path = sys.argv[1], sys.argv[2], sys.argv[3]
with open(src) as fh:
    docs = [d for d in yaml.safe_load_all(fh) if d]

services = [d for d in docs if d.get("kind") == "Service"]
rest = [d for d in docs if d.get("kind") != "Service"]

with open(services_path, "w") as fh:
    yaml.safe_dump_all(services, fh)
with open(rest_path, "w") as fh:
    yaml.safe_dump_all(rest, fh)
PY

# --- Step 5: Apply Services first ---
echo "→ NodeLocal DNSCache: applying Services (kube-dns-upstream + metrics)..."
kubectl_apply_if_changed -f "$SERVICES_FILE"

# Brief settle so the Service ClusterIP is allocated before the DaemonSet
# pods are created (kubelet snapshots Service env at pod-create time).
sleep 3

# --- Step 6: Apply ConfigMap, ServiceAccount, DaemonSet ---
echo "→ NodeLocal DNSCache: applying ConfigMap, ServiceAccount, DaemonSet..."
kubectl_apply_if_changed -f "$REST_FILE"

# --- Step 7: Idempotency safety net ---
# Only restart on first-time deploy: the DaemonSet pods created above (or
# any pre-existing ones from an earlier failed install) won't have the
# KUBE_DNS_UPSTREAM_SERVICE_HOST env var unless the Service existed before
# they were scheduled. On steady-state re-runs, env is already populated.
if [[ "$UPSTREAM_FIRST_DEPLOY" == "true" ]]; then
  echo "→ NodeLocal DNSCache: first-time deploy — rolling DaemonSet to inject upstream Service env..."
  kubectl rollout restart ds node-local-dns -n kube-system
else
  echo "→ NodeLocal DNSCache: not a first-time deploy — skipping rollout restart."
fi

echo "NodeLocal DNSCache deployed."
