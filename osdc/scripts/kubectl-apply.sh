#!/usr/bin/env bash
# shellcheck disable=SC2310
#
# Shared helper: skip kubectl apply when nothing has changed.
#
# Usage:
#   source "$OSDC_UPSTREAM/scripts/kubectl-apply.sh"
#   kubectl_apply_if_changed [kubectl apply args...]
#
# Runs kubectl diff first to check for changes. If the resources are
# already up-to-date, skips the apply entirely (no unnecessary writes,
# no spurious last-applied-configuration updates).
#
# Accepts the same arguments as kubectl apply: -f file.yaml, -k dir/,
# -f -, --server-side, --force-conflicts, etc.
#
# When -f - is used (piped input), stdin is buffered to a temp file so
# both diff and apply can read it.
#
# The function preserves the caller's set -e behavior: if kubectl apply
# fails, it returns non-zero so the caller's error handling applies.

kubectl_apply_if_changed() {
  local args=("$@")
  local tmpfile=""
  local label=""

  # Extract a descriptive label from the args for log messages
  for i in "${!args[@]}"; do
    case "${args[$i]}" in
      -f | -k)
        if [[ -n "${args[$((i + 1))]:-}" && "${args[$((i + 1))]}" != "-" ]]; then
          label="${args[$((i + 1))]}"
        fi
        ;;
      *) ;;
    esac
  done

  # Buffer stdin when -f - is used so both diff and apply can read it
  for i in "${!args[@]}"; do
    if [[ "${args[$i]}" == "-" ]]; then
      local prev_idx=$((i - 1))
      if [[ "${args[$prev_idx]:-}" == "-f" ]]; then
        tmpfile=$(mktemp)
        cat >"$tmpfile"
        args[i]="$tmpfile"
        [[ -z "$label" ]] && label="(stdin)"
      fi
    fi
  done

  label="${label:-resources}"

  # kubectl diff exit codes:
  #   0 = no differences
  #   1 = differences found
  #  >1 = error (e.g. resource doesn't exist yet, API error)
  local diff_rc=0
  kubectl diff "${args[@]}" &>/dev/null || diff_rc=$?

  local rc=0
  if [[ $diff_rc -eq 0 ]]; then
    echo "  No changes — skipping apply for ${label}."
  elif [[ $diff_rc -eq 1 ]]; then
    echo "  Changes detected — applying ${label}..."
    kubectl apply "${args[@]}" || rc=$?
  else
    echo "  Diff check failed (rc=${diff_rc}) — applying ${label}..."
    kubectl apply "${args[@]}" || rc=$?
  fi

  [[ -n "$tmpfile" ]] && rm -f "$tmpfile"
  return "$rc"
}
