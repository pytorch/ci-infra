#!/usr/bin/env bash
set -euo pipefail
#
# Delete every in-cluster resource left orphaned by the removal of the
# git-cache module. The git-cache codebase (manifests, scripts, image build)
# was deleted in a code-only PR; this script handles the cluster-side cleanup
# that was deliberately deferred to operators.
#
# Usage:
#   ./scripts/cleanup-git-cache.sh <cluster-id>
#
# Env vars:
#   OSDC_CONFIRM=yes   Required to actually delete. Without it the script
#                      runs in dry-run mode and only enumerates what would
#                      be removed.
#
# Prereqs (handled by this script — no operator action needed):
#   - `just kubeconfig <cluster-id>` is invoked internally to set the
#     kubeconfig context.
#
# What this deletes (everything named git-cache-* left over from the
# removed module):
#   - PodMonitor       git-cache-daemonset       -n monitoring
#   - ServiceMonitor   git-cache-central         -n monitoring
#   - DaemonSet        git-cache-warmer          -n kube-system
#   - StatefulSet      git-cache-central         -n kube-system
#   - PodDisruptionBudget git-cache-central      -n kube-system
#   - Services in kube-system selected by
#       app.kubernetes.io/name in (git-cache-central, git-cache-warmer)
#       and any svc named git-cache-* as a name-prefix fallback
#   - ConfigMaps in kube-system selected the same way
#   - ServiceAccount      git-cache-warmer       -n kube-system
#   - ClusterRole         git-cache-warmer       (cluster-scoped)
#   - ClusterRoleBinding  git-cache-warmer       (cluster-scoped)
#   - PVCs in kube-system labeled
#       app.kubernetes.io/name=git-cache-central, and any PVC named
#       git-cache-central-* as a name-prefix fallback (dedup'd)
#   - Node taint git-cache-not-ready on every node still carrying it
#
# Idempotency:
#   Safe to re-run. Every kubectl delete uses --ignore-not-found, and the
#   enumeration only emits resources that currently exist.
#
# Why not kustomize delete:
#   The git-cache manifests are gone from the repo, so kustomize can no
#   longer produce the manifest set required to delete-by-ref. This script
#   enumerates by name and label instead.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/mise-activate.sh"

CONFIG_PY="$SCRIPT_DIR/cluster-config.py"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# ── Argument parsing ────────────────────────────────────────────────────────

if [[ $# -ne 1 ]]; then
  echo "Usage: $0 <cluster-id>" >&2
  echo "" >&2
  echo "Available clusters:" >&2
  uv run "$CONFIG_PY" --list | sed 's/^/  /' >&2
  exit 2
fi
CLUSTER="$1"

CNAME=$(uv run "$CONFIG_PY" "$CLUSTER" cluster_name)
REGION=$(uv run "$CONFIG_PY" "$CLUSTER" region)

# ── Mode selection ─────────────────────────────────────────────────────────

CONFIRM="${OSDC_CONFIRM:-}"
if [[ "$CONFIRM" == "yes" ]]; then
  MODE="apply"
  TAG="[DELETE]"
else
  MODE="dry-run"
  TAG="[DRY-RUN]"
fi

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "git-cache cleanup for cluster: $CLUSTER ($CNAME) in $REGION"
echo "  Mode: $MODE"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# ── Kubeconfig ─────────────────────────────────────────────────────────────

(cd "$ROOT" && just kubeconfig "$CLUSTER")
echo ""

# ── kubectl wrapper (proxy bypass) ─────────────────────────────────────────

k() {
  NO_PROXY="${NO_PROXY:-},.eks.amazonaws.com" \
    no_proxy="${no_proxy:-},.eks.amazonaws.com" \
    kubectl "$@"
}

# ── Enumeration helpers ────────────────────────────────────────────────────
#
# Each enumerator prints zero or more lines on stdout in the form:
#   <scope>|<kind>|<namespace-or-empty>|<name>
# where <scope> is "namespaced" or "cluster" or "taint". The taint scope is
# special-cased because removal uses `kubectl taint`, not `kubectl delete`.

enumerate_named() {
  local kind="$1" ns="$2" name="$3"
  local out
  if [[ -n "$ns" ]]; then
    out=$(k get "$kind" "$name" -n "$ns" --ignore-not-found -o name 2>/dev/null || true)
  else
    out=$(k get "$kind" "$name" --ignore-not-found -o name 2>/dev/null || true)
  fi
  if [[ -n "$out" ]]; then
    # `out` is e.g. "podmonitor.monitoring.coreos.com/git-cache-daemonset".
    # We already know kind, ns, and name, so just emit the canonical row.
    if [[ -n "$ns" ]]; then
      echo "namespaced|$kind|$ns|$name"
    else
      echo "cluster|$kind|$ns|$name"
    fi
  fi
}

enumerate_by_selector_and_prefix() {
  # Enumerate every resource of <kind> in <ns> that either matches the git-cache
  # label selector OR has a name starting with "git-cache-". Dedup by name.
  local kind="$1" ns="$2"
  local by_label by_prefix all
  by_label=$(
    k get "$kind" -n "$ns" \
      -l 'app.kubernetes.io/name in (git-cache-central,git-cache-warmer)' \
      --ignore-not-found -o name 2>/dev/null | sed 's|^[^/]*/||' || true
  )
  by_prefix=$(
    k get "$kind" -n "$ns" --ignore-not-found -o name 2>/dev/null \
      | sed 's|^[^/]*/||' \
      | grep '^git-cache-' || true
  )
  all=$(printf "%s\n%s\n" "$by_label" "$by_prefix" | sed '/^$/d' | sort -u)
  if [[ -n "$all" ]]; then
    while IFS= read -r name; do
      echo "namespaced|$kind|$ns|$name"
    done <<<"$all"
  fi
}

enumerate_pvcs() {
  local ns="kube-system"
  local by_label by_prefix all
  by_label=$(
    k get pvc -n "$ns" \
      -l 'app.kubernetes.io/name=git-cache-central' \
      --ignore-not-found -o name 2>/dev/null | sed 's|^[^/]*/||' || true
  )
  by_prefix=$(
    k get pvc -n "$ns" --ignore-not-found -o name 2>/dev/null \
      | sed 's|^[^/]*/||' \
      | grep '^git-cache-central-' || true
  )
  all=$(printf "%s\n%s\n" "$by_label" "$by_prefix" | sed '/^$/d' | sort -u)
  if [[ -n "$all" ]]; then
    while IFS= read -r name; do
      echo "namespaced|pvc|$ns|$name"
    done <<<"$all"
  fi
}

enumerate_tainted_nodes() {
  local out
  out=$(
    k get nodes \
      -o custom-columns=NAME:.metadata.name,TAINTS:.spec.taints[*].key \
      --no-headers 2>/dev/null \
      | awk '$2 ~ /git-cache-not-ready/ {print $1}' || true
  )
  if [[ -n "$out" ]]; then
    while IFS= read -r name; do
      echo "taint|node|$name|git-cache-not-ready"
    done <<<"$out"
  fi
}

enumerate_all() {
  enumerate_named podmonitor monitoring git-cache-daemonset
  enumerate_named servicemonitor monitoring git-cache-central
  enumerate_named daemonset kube-system git-cache-warmer
  enumerate_named statefulset kube-system git-cache-central
  enumerate_named pdb kube-system git-cache-central
  enumerate_by_selector_and_prefix svc kube-system
  enumerate_by_selector_and_prefix configmap kube-system
  enumerate_named serviceaccount kube-system git-cache-warmer
  enumerate_named clusterrole "" git-cache-warmer
  enumerate_named clusterrolebinding "" git-cache-warmer
  enumerate_pvcs
  enumerate_tainted_nodes
}

# ── Print enumeration ──────────────────────────────────────────────────────

echo "━━━ Enumerating git-cache resources ━━━"
RESOURCES=$(enumerate_all || true)
if [[ -z "$RESOURCES" ]]; then
  echo "$TAG (none) — no git-cache resources found."
else
  while IFS='|' read -r scope kind ns name; do
    if [[ "$scope" == "taint" ]]; then
      echo "$TAG taint $name on node/$ns"
    elif [[ -n "$ns" ]]; then
      echo "$TAG $kind/$name -n $ns"
    else
      echo "$TAG $kind/$name (cluster-scoped)"
    fi
  done <<<"$RESOURCES"
fi
COUNT=$(printf "%s\n" "$RESOURCES" | sed '/^$/d' | wc -l | tr -d ' ')
echo ""
echo "Total: $COUNT resource(s)/taint(s) found."
echo ""

# ── Dry-run: stop here ─────────────────────────────────────────────────────

if [[ "$MODE" == "dry-run" ]]; then
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo "Dry-run complete. No changes made."
  echo "Re-run with OSDC_CONFIRM=yes to actually delete:"
  echo "  OSDC_CONFIRM=yes $0 $CLUSTER"
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  exit 0
fi

# ── Apply: optional interactive confirmation ───────────────────────────────

if [[ "$COUNT" == "0" ]]; then
  echo "Nothing to delete. Exiting."
  exit 0
fi

if [[ -t 0 ]]; then
  read -rp "Proceed with deletion of $COUNT item(s) on cluster '$CLUSTER'? (y/N): " ANS
  if [[ "$ANS" != "y" && "$ANS" != "Y" ]]; then
    echo "Aborted by operator."
    exit 1
  fi
fi
echo ""

# ── Apply: perform deletion ────────────────────────────────────────────────

echo "━━━ Deleting ━━━"
DELETED=0
while IFS='|' read -r scope kind ns name; do
  [[ -z "$scope" ]] && continue
  case "$scope" in
    namespaced)
      if [[ "$kind" == "pvc" ]]; then
        k delete --ignore-not-found --wait=false "$kind/$name" -n "$ns" || true
      else
        k delete --ignore-not-found "$kind/$name" -n "$ns" || true
      fi
      DELETED=$((DELETED + 1))
      ;;
    cluster)
      k delete --ignore-not-found "$kind/$name" || true
      DELETED=$((DELETED + 1))
      ;;
    taint)
      # ns column holds the node name; name column holds the taint key.
      k taint node "$ns" "${name}-" || true
      DELETED=$((DELETED + 1))
      ;;
    *)
      echo "internal error: unknown scope '$scope'" >&2
      exit 3
      ;;
  esac
done <<<"$RESOURCES"
echo ""
echo "Issued $DELETED deletion command(s)."
echo ""

# ── Post-apply verification ────────────────────────────────────────────────

echo "━━━ Verifying ━━━"
REMAINING=$(enumerate_all || true)
if [[ -z "$REMAINING" ]]; then
  echo "All git-cache resources removed."
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  exit 0
fi

echo "STILL PRESENT:"
while IFS='|' read -r scope kind ns name; do
  if [[ "$scope" == "taint" ]]; then
    echo "  taint $name on node/$ns"
  elif [[ -n "$ns" ]]; then
    echo "  $kind/$name -n $ns"
  else
    echo "  $kind/$name (cluster-scoped)"
  fi
done <<<"$REMAINING"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
exit 2
