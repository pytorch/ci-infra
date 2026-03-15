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
