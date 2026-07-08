#!/usr/bin/env bash
set -euo pipefail
#
# Optimized ARC Runners — delegates to upstream deploy with local definitions.
# Called by: just deploy-module <cluster> arc-runners-opt

MODULE_DIR="$(cd "$(dirname "$0")" && pwd)"
UPSTREAM_ROOT="${OSDC_UPSTREAM:-$(cd "$MODULE_DIR/../.." && pwd)}"

export ARC_RUNNERS_DEFS_DIR="$MODULE_DIR/defs"
export ARC_RUNNERS_OUTPUT_DIR="$MODULE_DIR/generated"
export ARC_RUNNERS_MODULE_NAME="arc-runners-opt"
export NODEPOOLS_DEFS_DIR="$UPSTREAM_ROOT/modules/nodepools-opt/defs"
exec "$UPSTREAM_ROOT/modules/arc-runners/deploy.sh" "$@"
