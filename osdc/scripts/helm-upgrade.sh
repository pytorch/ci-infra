#!/usr/bin/env bash
# shellcheck disable=SC2310
#
# Shared helper: skip helm upgrade when nothing has changed.
#
# Usage:
#   source "$OSDC_UPSTREAM/scripts/helm-upgrade.sh"
#   helm_upgrade_if_changed <release> <namespace> [helm upgrade flags...]
#
# Compares the rendered template against the live release manifest.
# If they're identical, skips the upgrade entirely (no new revision,
# no rolling restart, no --wait delay).
#
# Supports all helm upgrade flags: -f, --set, --version, --timeout,
# --wait, --create-namespace, oci:// charts, etc.
#
# The function preserves the caller's set -e behavior: if the upgrade
# fails, it returns non-zero so the caller's error handling applies.

helm_upgrade_if_changed() {
  local release="$1"
  local namespace="$2"
  shift 2
  # Remaining args are the full set of helm upgrade flags

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

  if ! helm template "$release" --namespace "$namespace" "${template_args[@]}" >"$proposed" 2>/dev/null; then
    # Template rendering failed — fall through to normal upgrade
    # which will produce a proper error message
    rm -f "$proposed"
    echo "  Template render failed — running full upgrade..."
    helm upgrade --install "$release" --namespace "$namespace" "$@"
    return $?
  fi

  # Get the live manifest
  local current
  current=$(mktemp)
  helm get manifest "$release" -n "$namespace" >"$current" 2>/dev/null || true

  # Compare (ignore whitespace and comment-only differences)
  # Sort YAML documents for stable comparison
  local changed=false
  if ! diff -q <(grep -v '^#' "$proposed" | sed '/^$/d' | sort) \
    <(grep -v '^#' "$current" | sed '/^$/d' | sort) \
    &>/dev/null; then
    changed=true
  fi

  rm -f "$proposed" "$current"

  if $changed; then
    echo "  Changes detected — upgrading '$release'..."
    helm upgrade --install "$release" --namespace "$namespace" "$@"
    return $?
  else
    echo "  No changes detected — skipping '$release' upgrade."
    return 0
  fi
}
