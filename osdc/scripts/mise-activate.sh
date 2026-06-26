#!/usr/bin/env bash
# Source this to ensure mise-managed tools (tofu, kubectl, helm, crane, uv, etc.)
# are on PATH. Required for justfile shebang recipes and standalone scripts,
# which bypass the justfile's `set shell := ["mise", "exec", ...]` directive.
#
# Usage (from justfile shebang recipe):
#   source "{{ROOT}}/scripts/mise-activate.sh"
#
# Usage (from standalone script):
#   source "$(dirname "$0")/../../scripts/mise-activate.sh"
if command -v mise &>/dev/null; then
  _MISE_ROOT="${OSDC_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
  eval "$(mise env -s bash -C "$_MISE_ROOT" 2>/dev/null)" || true
  unset _MISE_ROOT
fi

# Ensure the OpenTofu plugin cache dir exists before any `tofu init`.
# mise.toml points TF_PLUGIN_CACHE_DIR at {{config_root}}/.terraform.d/plugin-cache,
# but tofu requires the directory to pre-exist (it won't create it) and aborts
# init with "plugin cache dir ... cannot be opened" otherwise. Since .terraform.d/
# is gitignored, a fresh `git worktree` checkout starts without it — so create it
# idempotently here, the single chokepoint every tofu-running recipe sources.
if [[ -n "${TF_PLUGIN_CACHE_DIR:-}" ]]; then
  mkdir -p "${TF_PLUGIN_CACHE_DIR}"
fi
