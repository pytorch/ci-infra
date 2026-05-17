#!/usr/bin/env bash
set -euo pipefail
#
# ARC Runners module deploy script.
# Called by: just deploy-module <cluster> arc-runners
# Args: $1=cluster-id  $2=cluster-name  $3=region
#
# Requires: 'arc' module must be deployed first (ARC controller).
# Generates ARC runner scale set configs from defs + template,
# then installs each as a Helm release.
#
# Environment (optional — consumers can override to use local defs):
#   ARC_RUNNERS_DEFS_DIR    — directory containing runner definitions
#   ARC_RUNNERS_OUTPUT_DIR  — directory for generated runner configs
#   ARC_RUNNERS_TEMPLATE    — path to runner.yaml.tpl template
#   ARC_RUNNERS_MODULE_NAME — label value for osdc.io/module (default: "arc-runners")

CLUSTER="$1"
export CNAME="$2"
export REGION="$3"
MODULE_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="${OSDC_ROOT:-$(cd "$MODULE_DIR/../.." && pwd)}"
UPSTREAM_ROOT="${OSDC_UPSTREAM:-$REPO_ROOT}"
# shellcheck source=/dev/null
source "$UPSTREAM_ROOT/scripts/mise-activate.sh"
# shellcheck source=/dev/null
source "$UPSTREAM_ROOT/scripts/helm-upgrade.sh"
# shellcheck source=/dev/null
source "$UPSTREAM_ROOT/scripts/kubectl-apply.sh"
CFG="$UPSTREAM_ROOT/scripts/cluster-config.py"

# Chart version — MUST match the controller (arc module). Read from clusters.yaml.
ARC_CHART_VERSION=$(uv run "$CFG" "$CLUSTER" arc.chart_version 0.14.1)

# Allow consumers to override defs, output, template, and module name
DEFS_DIR="${ARC_RUNNERS_DEFS_DIR:-$MODULE_DIR/defs}"
OUTPUT_DIR="${ARC_RUNNERS_OUTPUT_DIR:-$MODULE_DIR/generated}"
TEMPLATE="${ARC_RUNNERS_TEMPLATE:-$MODULE_DIR/templates/runner.yaml.tpl}"
MODULE_NAME="${ARC_RUNNERS_MODULE_NAME:-arc-runners}"

# --- Preflight: ARC module must be enabled ---
if ! uv run "$CFG" "$CLUSTER" has-module arc; then
  echo "ERROR: arc-runners requires the 'arc' module."
  echo "Cluster '$CLUSTER' does not have 'arc' in its module list."
  echo "If you only need compute provisioning (NodePools), use the 'nodepools' module instead."
  exit 1
fi

# --- Step 1: Generate ARC runner configs ---
echo "Generating ARC runner configs from definitions..."
ARC_RUNNERS_DEFS_DIR="$DEFS_DIR" \
  ARC_RUNNERS_OUTPUT_DIR="$OUTPUT_DIR" \
  ARC_RUNNERS_TEMPLATE="$TEMPLATE" \
  ARC_RUNNERS_MODULE_NAME="$MODULE_NAME" \
  uv run "$MODULE_DIR/scripts/python/generate_runners.py" "$CLUSTER"

# --- Step 2: Validate runner configs (Guaranteed QoS) ---
echo ""
echo "Validating runner configurations..."
ARC_RUNNERS_OUTPUT_DIR="$OUTPUT_DIR" \
  uv run "$MODULE_DIR/scripts/python/validate_runner_qos.py"

# --- Step 3: Apply ARC runner scale sets (parallel) ---
MAX_PARALLEL="${ARC_RUNNERS_MAX_PARALLEL:-10}"
echo ""
echo "Deploying ARC runner scale sets (max ${MAX_PARALLEL} parallel)..."

LOGDIR=$(mktemp -d)
PIDS=()
NAMES=()

cleanup() {
  for pid in "${PIDS[@]:-}"; do
    kill "$pid" 2>/dev/null || true
  done
  wait 2>/dev/null || true
  rm -rf "$LOGDIR"
}
trap cleanup EXIT

deploy_one_runner() {
  local runner_config="$1"
  local runner_name
  runner_name=$(basename "$runner_config" .yaml)
  # Normalize: dots/underscores → dashes (must match ConfigMap naming)
  runner_name="${runner_name//[._]/-}"
  local logfile="$LOGDIR/${runner_name}.log"

  {
    # Apply ConfigMap (second YAML doc — job pod hook template)
    awk '/^---$/,0' "$runner_config" | kubectl_apply_if_changed -f -

    # Install Helm chart (first YAML doc — ARC scale set values)
    local tmpfile="/tmp/${runner_name}-values-$$.yaml"
    awk 'BEGIN{doc=0} /^---$/{doc++} doc==0' "$runner_config" >"$tmpfile"

    helm_upgrade_if_changed "arc-${runner_name}" arc-runners \
      --history-max 2 \
      -f "$tmpfile" \
      --set template.spec.securityContext.runAsUser=1000 \
      --set template.spec.securityContext.runAsGroup=1000 \
      --set template.spec.securityContext.fsGroup=1000 \
      oci://ghcr.io/jeanschmidt/actions-runner-controller-charts/gha-runner-scale-set \
      --version "${ARC_CHART_VERSION}" \
      --wait

    rm -f "$tmpfile"
  } >"$logfile" 2>&1
}

shopt -s nullglob
_generated=("$OUTPUT_DIR/"*.yaml)
shopt -u nullglob
if ((${#_generated[@]} == 0)); then
  echo "ERROR: No generated runner YAML files in $OUTPUT_DIR"
  exit 1
fi

for runner_config in "${_generated[@]}"; do
  runner_name=$(basename "$runner_config" .yaml)
  # Normalize for log file matching (must match deploy_one_runner's normalization)
  runner_name_normalized="${runner_name//[._]/-}"
  echo "  → ${runner_name}"

  deploy_one_runner "$runner_config" &
  PIDS+=($!)
  NAMES+=("$runner_name_normalized")

  # Concurrency limiter: wait for a slot if at max
  while (($(jobs -rp | wc -l) >= MAX_PARALLEL)); do
    sleep 0.5
  done
done

# Wait for all and collect failures
FAILED=()
for i in "${!PIDS[@]}"; do
  if ! wait "${PIDS[$i]}"; then
    FAILED+=("${NAMES[$i]}")
  fi
done

# Print logs for any failures
if ((${#FAILED[@]} > 0)); then
  echo ""
  echo "ERROR: ${#FAILED[@]} runner(s) failed to deploy:"
  for name in "${FAILED[@]}"; do
    echo "  ✗ ${name}"
    sed 's/^/    /' <"$LOGDIR/${name}.log"
  done
  exit 1
fi

echo ""
echo "ARC runners deployed."

# --- Step 4: Clean up stale ARC runners ---
echo ""
echo "Checking for stale ARC runners (module: ${MODULE_NAME})..."

# Build set of expected normalized names from generated files
# Normalize: dots/underscores → dashes (must match ConfigMap naming convention)
EXPECTED_RUNNERS=()
for f in "${_generated[@]}"; do
  raw_name="$(basename "$f" .yaml)"
  EXPECTED_RUNNERS+=("${raw_name//[._]/-}")
done

# Query cluster for ConfigMaps with our module label
DEPLOYED_CMS=$(kubectl get configmaps -n arc-runners \
  -l "osdc.io/module=${MODULE_NAME}" \
  -o jsonpath='{.items[*].metadata.name}' 2>/dev/null || echo "")

STALE_COUNT=0
STALE_RELEASES=()
for cm in $DEPLOYED_CMS; do
  # ConfigMap name format: arc-runner-hook-{normalized_name}
  # Strip prefix to get the normalized runner name
  local_name="${cm#arc-runner-hook-}"

  is_expected=false
  for expected in "${EXPECTED_RUNNERS[@]}"; do
    if [[ "$local_name" == "$expected" ]]; then
      is_expected=true
      break
    fi
  done
  if ! $is_expected; then
    STALE_RELEASES+=("arc-${local_name}")
    echo "  Deleting stale Helm release: arc-${local_name}"
    helm uninstall "arc-${local_name}" -n arc-runners --wait=false 2>/dev/null \
      || echo "    WARNING: Failed to uninstall Helm release arc-${local_name} (continuing)"
    echo "  Deleting stale ConfigMap: $cm"
    kubectl delete configmap "$cm" -n arc-runners 2>/dev/null \
      || echo "    WARNING: Failed to delete ConfigMap $cm (continuing)"
    STALE_COUNT=$((STALE_COUNT + 1))
  fi
done

if ((STALE_COUNT > 0)); then
  echo "Cleaned up $STALE_COUNT stale runner(s)."
else
  echo "No stale runners found."
fi

# --- Step 5: Clean up orphaned Helm release history secrets ---
# helm uninstall (Step 4) removes only the current "deployed" revision secret.
# Prior "superseded" and "failed" revision secrets remain as orphans in etcd.
# Delete them for each release that was just uninstalled by Step 4.
if ((${#STALE_RELEASES[@]} > 0)); then
  echo ""
  echo "Cleaning up Helm release history secrets for ${#STALE_RELEASES[@]} uninstalled release(s)..."
  ORPHAN_COUNT=0
  for release in "${STALE_RELEASES[@]}"; do
    while IFS= read -r secret_name; do
      [[ -z "$secret_name" ]] && continue
      echo "  Deleting orphaned secret: $secret_name (release=$release)"
      kubectl delete secret "$secret_name" -n arc-runners 2>/dev/null \
        || echo "    WARNING: Failed to delete secret $secret_name (continuing)"
      ORPHAN_COUNT=$((ORPHAN_COUNT + 1))
    done < <(kubectl get secrets -n arc-runners \
      -l "owner=helm,name=${release}" \
      -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' 2>/dev/null || true)
  done
  if ((ORPHAN_COUNT > 0)); then
    echo "Cleaned up $ORPHAN_COUNT orphaned Helm history secret(s)."
  else
    echo "No orphaned Helm history secrets found."
  fi
fi

# --- Step 6: Detect stale AutoscalingListeners ---
LISTENER_CHECK="${ARC_LISTENER_CHECK:-enabled}"
case "$LISTENER_CHECK" in
  skip | SKIP | no | NO | false | FALSE)
    echo "  → Skipping stale listener check (ARC_LISTENER_CHECK=skip)"
    ;;
  *)
    LISTENER_TIMEOUT="${ARC_LISTENER_CHECK_TIMEOUT:-60}"
    LISTENER_SELECTOR="app.kubernetes.io/component=runner-scale-set-listener"
    LISTENER_NS="arc-systems"

    if ! kubectl get pods -n "$LISTENER_NS" -l "$LISTENER_SELECTOR" \
      -o name >/dev/null 2>&1; then
      echo "  → Listener check skipped (kubectl error)"
    else
      _initial=$(kubectl get pods -n "$LISTENER_NS" -l "$LISTENER_SELECTOR" \
        -o name 2>/dev/null || true)
      if [[ -z "$_initial" ]]; then
        echo "  → Listener check: no listeners deployed yet."
      else
        echo "  → Waiting up to ${LISTENER_TIMEOUT}s for listener pods to settle..."

        _elapsed=0
        while ((_elapsed < LISTENER_TIMEOUT)); do
          _all_settled=true
          while IFS=$'\t' read -r _phase _restart; do
            [[ -z "$_phase" ]] && continue
            if [[ "$_phase" != "Running" ]] && ((_restart < 2)); then
              _all_settled=false
              break
            fi
          done < <(kubectl get pods -n "$LISTENER_NS" -l "$LISTENER_SELECTOR" \
            -o jsonpath='{range .items[*]}{.status.phase}{"\t"}{.status.containerStatuses[0].restartCount}{"\n"}{end}' \
            2>/dev/null || true)

          if $_all_settled; then
            break
          fi
          sleep 5
          _elapsed=$((_elapsed + 5))
        done

        STALE_LISTENERS=()
        STALE_ARS=()

        while IFS=$'\t' read -r _pod _phase _restart _ars; do
          [[ -z "$_pod" ]] && continue
          _restart="${_restart:-0}"
          if [[ "$_phase" != "Running" ]] || ((_restart >= 2)); then
            _logs=$(kubectl logs -n "$LISTENER_NS" "$_pod" --previous --tail=200 2>/dev/null || true)
            if [[ -z "$_logs" ]]; then
              _logs=$(kubectl logs -n "$LISTENER_NS" "$_pod" --tail=200 2>/dev/null || true)
            fi
            if echo "$_logs" | grep -q "RunnerScaleSetNotFoundException"; then
              _ars_name="$_ars"
              if [[ -z "$_ars_name" ]]; then
                _ars_name="${_pod%-*-listener}"
              fi
              STALE_LISTENERS+=("$_pod")
              STALE_ARS+=("$_ars_name")
            fi
          fi
        done < <(kubectl get pods -n "$LISTENER_NS" -l "$LISTENER_SELECTOR" \
          -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.status.phase}{"\t"}{.status.containerStatuses[0].restartCount}{"\t"}{.metadata.labels.actions\.github\.com/scale-set-name}{"\n"}{end}' \
          2>/dev/null || true)

        if ((${#STALE_LISTENERS[@]} == 0)); then
          echo "  → Listener check: no stale scale sets detected."
        else
          N=${#STALE_LISTENERS[@]}
          echo ""
          echo "#######################################################################"
          echo "#"
          echo "#  WARNING: STALE AUTOSCALINGLISTENERS DETECTED"
          echo "#"
          echo "#  ${N} listener(s) are crash-looping with RunnerScaleSetNotFoundException."
          echo "#  The runner scale set has been deleted on the GitHub side (or the"
          echo "#  GitHub App lost access to the repo). The ARC controller will NOT"
          echo "#  auto-recover — the listener will keep crash-looping with the stale"
          echo "#  scale set ID until you re-register."
          echo "#"
          echo "#  Affected:"
          for _i in "${!STALE_LISTENERS[@]}"; do
            echo "#    - ${STALE_LISTENERS[$_i]}  →  ARS: ${STALE_ARS[$_i]}"
          done
          echo "#"
          echo "#  Before healing, verify the GitHub App still has access to the repo:"
          echo "#    gh api /repos/<org>/<repo> | jq '.archived, .name'"
          echo "#"
          echo "#  To heal a single ARS:"
          echo "#    just heal-listeners ${CLUSTER} <ars-name>"
          echo "#"
          echo "#  To heal ALL crash-looping listeners on this cluster:"
          echo "#    just heal-listeners ${CLUSTER}"
          echo "#"
          echo "#######################################################################"
          echo ""
        fi
      fi
    fi
    ;;
esac
