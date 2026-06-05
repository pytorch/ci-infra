#!/usr/bin/env bash
set -euo pipefail
#
# Deploy the node-taint-remover shared library as a ConfigMap.
# Called from the justfile's deploy-base recipe.
#
# Args: $1=cluster-id
#
# Generates `node-taint-remover-lib` ConfigMap in kube-system from
# lib/taint_remover.py. This ConfigMap is mounted into other DaemonSets
# (cache-enforcer, registry-mirror-config, node-performance-tuning) so
# they can remove their own startup taint after init completes.
#
# The .py file lives in the repo for IDE/test ergonomics; the ConfigMap
# is the deployment artifact. Generating from the .py file at apply time
# guarantees the two cannot drift.

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

LIB_PY="$SCRIPT_DIR/lib/taint_remover.py"
CM_NAME="node-taint-remover-lib"
NS="kube-system"

if [[ ! -f "$LIB_PY" ]]; then
  echo "ERROR: missing $LIB_PY" >&2
  exit 1
fi

# --- Validate the .py file parses as valid Python before shipping it ---
echo "→ node-taint-remover: validating taint_remover.py syntax..."
python3 -c "import ast,sys; ast.parse(open(sys.argv[1]).read())" "$LIB_PY"

# --- Cleanup trap ---
WORK_DIR=""
cleanup() {
  [[ -n "$WORK_DIR" ]] && rm -rf "$WORK_DIR" 2>/dev/null || true
}
trap cleanup EXIT

WORK_DIR=$(mktemp -d)
MANIFEST="$WORK_DIR/configmap.yaml"

# --- Generate ConfigMap from the .py file ---
echo "→ node-taint-remover: rendering ConfigMap '$CM_NAME' from $(basename "$LIB_PY")..."
kubectl create configmap "$CM_NAME" \
  --from-file="taint_remover.py=$LIB_PY" \
  -n "$NS" \
  --dry-run=client \
  -o yaml >"$MANIFEST"

# Inject labels so the ConfigMap is identifiable like other base components.
# kubectl create configmap doesn't accept --labels in older versions, so we
# patch them in after rendering.
python3 - "$MANIFEST" <<'PY'
import sys
import yaml

path = sys.argv[1]
with open(path) as fh:
    doc = yaml.safe_load(fh)
doc.setdefault("metadata", {}).setdefault("labels", {}).update({
    "app.kubernetes.io/name": "node-taint-remover",
    "app.kubernetes.io/component": "shared-library",
})
with open(path, "w") as fh:
    yaml.safe_dump(doc, fh, sort_keys=False)
PY

# --- Apply ---
echo "→ node-taint-remover: applying ConfigMap..."
kubectl_apply_if_changed -f "$MANIFEST"

echo "node-taint-remover deployed."
