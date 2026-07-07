#!/usr/bin/env bash
set -euo pipefail
#
# Optimized NodePools — delegates to upstream deploy with local definitions.
# Called by: just deploy-module <cluster> nodepools-opt

MODULE_DIR="$(cd "$(dirname "$0")" && pwd)"
UPSTREAM_ROOT="${OSDC_UPSTREAM:-$(cd "$MODULE_DIR/../.." && pwd)}"

export NODEPOOLS_DEFS_DIR="$MODULE_DIR/defs"
export NODEPOOLS_OUTPUT_DIR="$MODULE_DIR/generated"
export NODEPOOLS_MODULE_NAME="nodepools-opt"
exec "$UPSTREAM_ROOT/modules/nodepools/deploy.sh" "$@"
