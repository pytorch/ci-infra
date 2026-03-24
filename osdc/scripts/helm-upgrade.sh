#!/usr/bin/env bash
# shellcheck disable=SC2310
#
# Shared helpers: skip helm upgrades when nothing has changed.
#
# Two strategies are provided:
#
#   helm_upgrade_if_changed  — Template-based comparison (YAML diff)
#     Compares `helm template` output against `helm get manifest`.
#     Works well for charts with deterministic templates. NOT suitable
#     for charts that use randAlphaNum, genCA, or other non-deterministic
#     template functions (every render produces different output → false
#     positives on every deploy).
#
#   helm_upgrade_by_input_hash  — Input-hash comparison (Flux-style)
#     Hashes all inputs (values files, --set args, chart version) and
#     stores the hash in a ConfigMap. Compares hashes on next deploy.
#     Immune to non-deterministic template functions. Use this for
#     charts like Harbor that generate random secrets.
#
# Usage:
#   source "$OSDC_UPSTREAM/scripts/helm-upgrade.sh"
#   helm_upgrade_if_changed <release> <namespace> [helm upgrade flags...]
#   helm_upgrade_by_input_hash <release> <namespace> [helm upgrade flags...]
#
# Both support all helm upgrade flags: -f, --set, --set-file, --version,
# --timeout, --wait, --create-namespace, oci:// charts, etc.
#
# Set HELM_FORCE_UPGRADE=1 to bypass both skip mechanisms.
# Set HELM_DIFF_DEBUG=1 to show debug info when changes are detected.

# --- Input-hash strategy (recommended for non-deterministic charts) ---

helm_upgrade_by_input_hash() {
  local release="$1"
  local namespace="$2"
  shift 2

  # Force upgrade if requested
  if [[ "${HELM_FORCE_UPGRADE:-}" == "1" ]]; then
    echo "  HELM_FORCE_UPGRADE set — upgrading '$release'..."
    helm upgrade --install "$release" --namespace "$namespace" "$@"
    return $?
  fi

  # Check if the release exists at all — if not, always install
  if ! helm status "$release" -n "$namespace" &>/dev/null; then
    echo "  Release '$release' not found — installing..."
    helm upgrade --install "$release" --namespace "$namespace" "$@"
    local rc=$?
    [[ $rc -eq 0 ]] && _store_input_hash "$release" "$namespace" "$@"
    return "$rc"
  fi

  # Compute hash of all inputs that affect the upgrade
  local new_hash
  new_hash=$(_compute_input_hash "$@")

  # Fetch stored hash from previous deploy
  local stored_hash
  stored_hash=$(kubectl get configmap "osdc-helm-hash-${release}" \
    -n "$namespace" -o jsonpath='{.data.hash}' 2>/dev/null || echo "")

  if [[ "$new_hash" == "$stored_hash" ]]; then
    echo "  No input changes — skipping '$release' upgrade."
    return 0
  fi

  if [[ "${HELM_DIFF_DEBUG:-}" == "1" ]]; then
    echo "  [DEBUG] Input hash changed: ${stored_hash:-<none>} → $new_hash" >&2
  fi

  echo "  Input changes detected — upgrading '$release'..."
  helm upgrade --install "$release" --namespace "$namespace" "$@"
  local rc=$?
  [[ $rc -eq 0 ]] && _store_input_hash "$release" "$namespace" "$@"
  return "$rc"
}

# Compute a SHA-256 hash of all Helm upgrade inputs:
# - Content of values files (-f / --values)
# - All --set, --set-string, --set-json, --set-file args (sorted for stability)
# - Chart reference and --version
_compute_input_hash() {
  local hash_input=""
  local skip_next=false
  local next_is_values=false
  local next_is_set=""
  local next_is_set_file=false
  local next_is_version=false
  local sets=()

  for arg in "$@"; do
    if $skip_next; then
      skip_next=false
      continue
    fi
    if $next_is_values; then
      next_is_values=false
      # Hash the file content, not the path (path may differ across machines)
      if [[ -f "$arg" ]]; then
        hash_input+="values:$(sha256sum "$arg" | cut -c1-64);"
      fi
      continue
    fi
    if [[ -n "$next_is_set" ]]; then
      sets+=("${next_is_set}:$arg")
      next_is_set=""
      continue
    fi
    if $next_is_set_file; then
      next_is_set_file=false
      # Hash the file content for --set-file args
      local key="${arg%%=*}"
      local file="${arg#*=}"
      if [[ -f "$file" ]]; then
        sets+=("set-file:${key}=$(sha256sum "$file" | cut -c1-64)")
      else
        sets+=("set-file:$arg")
      fi
      continue
    fi
    if $next_is_version; then
      next_is_version=false
      hash_input+="version:$arg;"
      continue
    fi

    case "$arg" in
      -f | --values)
        next_is_values=true
        ;;
      -f=* | --values=*)
        local file="${arg#*=}"
        [[ -f "$file" ]] && hash_input+="values:$(sha256sum "$file" | cut -c1-64);"
        ;;
      --set)
        next_is_set="set"
        ;;
      --set-string)
        next_is_set="set-string"
        ;;
      --set-json)
        next_is_set="set-json"
        ;;
      --set=*)
        sets+=("set:${arg#*=}")
        ;;
      --set-string=*)
        sets+=("set-string:${arg#*=}")
        ;;
      --set-json=*)
        sets+=("set-json:${arg#*=}")
        ;;
      --set-file)
        next_is_set_file=true
        ;;
      --set-file=*)
        local val="${arg#*=}"
        local key="${val%%=*}"
        local file="${val#*=}"
        if [[ -f "$file" ]]; then
          sets+=("set-file:${key}=$(sha256sum "$file" | cut -c1-64)")
        else
          sets+=("set-file:$val")
        fi
        ;;
      --version)
        next_is_version=true
        ;;
      --version=*)
        hash_input+="version:${arg#*=};"
        ;;
      # Skip flags that don't affect rendered output
      --wait | --create-namespace | --timeout | --timeout=* | \
        --history-max | --history-max=* | --burst-limit | --burst-limit=* | \
        --qps | --qps=*)
        case "$arg" in
          --timeout | --history-max | --burst-limit | --qps)
            skip_next=true
            ;;
          *) ;;
        esac
        ;;
      *)
        # Chart reference (e.g., harbor/harbor, oci://...) or other positional arg
        hash_input+="arg:$arg;"
        ;;
    esac
  done

  # Sort --set args for stability (order shouldn't matter for same result)
  local sorted_sets
  sorted_sets=$(printf '%s\n' "${sets[@]}" | sort)
  hash_input+="$sorted_sets"

  echo -n "$hash_input" | sha256sum | cut -c1-64
}

# Store the input hash in a ConfigMap for the next deploy to compare against
_store_input_hash() {
  local release="$1"
  local namespace="$2"
  shift 2
  local hash
  hash=$(_compute_input_hash "$@")
  if ! kubectl create configmap "osdc-helm-hash-${release}" \
    -n "$namespace" --from-literal=hash="$hash" \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null 2>&1; then
    echo "  Warning: failed to store input hash for '$release' — next deploy will re-upgrade" >&2
  fi
}

# --- Template-based strategy (for deterministic charts) ---

helm_upgrade_if_changed() {
  local release="$1"
  local namespace="$2"
  shift 2
  # Remaining args are the full set of helm upgrade flags

  # Force upgrade if requested
  if [[ "${HELM_FORCE_UPGRADE:-}" == "1" ]]; then
    echo "  HELM_FORCE_UPGRADE set — upgrading '$release'..."
    helm upgrade --install "$release" --namespace "$namespace" "$@"
    return $?
  fi

  # Check if the release exists at all — if not, always install
  if ! helm status "$release" -n "$namespace" &>/dev/null; then
    echo "  Release '$release' not found — installing..."
    helm upgrade --install "$release" --namespace "$namespace" "$@"
    return $?
  fi

  # Render the proposed template
  local proposed
  proposed=$(mktemp)
  # Build template args: strip --wait, --timeout, --history-max,
  # --create-namespace (not valid for helm template)
  local template_args=()
  local skip_next=false
  for arg in "$@"; do
    if $skip_next; then
      skip_next=false
      continue
    fi
    case "$arg" in
      --wait | --create-namespace)
        continue
        ;;
      --timeout | --history-max)
        skip_next=true
        continue
        ;;
      --timeout=* | --history-max=*)
        continue
        ;;
      --burst-limit | --qps)
        skip_next=true
        continue
        ;;
      --burst-limit=* | --qps=*)
        continue
        ;;
      *)
        template_args+=("$arg")
        ;;
    esac
  done

  local template_stderr
  template_stderr=$(mktemp)
  if ! helm template "$release" --namespace "$namespace" --no-hooks "${template_args[@]}" >"$proposed" 2>"$template_stderr"; then
    # Template rendering failed — fall through to normal upgrade
    rm -f "$proposed"
    echo "  Template render failed — running full upgrade..."
    cat "$template_stderr" >&2
    rm -f "$template_stderr"
    helm upgrade --install "$release" --namespace "$namespace" "$@"
    return $?
  fi

  # Get the live manifest
  local current
  current=$(mktemp)
  helm get manifest "$release" -n "$namespace" >"$current" 2>/dev/null || true

  # Compare using YAML-aware diff (handles key ordering, whitespace, comments)
  local changed=false
  local script_dir
  script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  if ! uv run "$script_dir/yaml-diff.py" "$proposed" "$current" 2>/dev/null; then
    changed=true
    if [[ "${HELM_DIFF_DEBUG:-}" == "1" ]]; then
      echo "  [DEBUG] Diff between proposed and current:" >&2
      diff <(grep -v '^#' "$proposed" | sort) <(grep -v '^#' "$current" | sort) >&2 || true
    fi
  fi

  rm -f "$proposed" "$current" "$template_stderr"

  if $changed; then
    echo "  Changes detected — upgrading '$release'..."
    helm upgrade --install "$release" --namespace "$namespace" "$@"
    return $?
  else
    echo "  No changes detected — skipping '$release' upgrade."
    return 0
  fi
}
