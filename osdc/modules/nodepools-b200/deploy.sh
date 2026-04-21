#!/usr/bin/env bash
set -euo pipefail
#
# B200 GPU NodePools — delegates to upstream deploy with local definitions.
# Called by: just deploy-module <cluster> nodepools-b200

MODULE_DIR="$(cd "$(dirname "$0")" && pwd)"
UPSTREAM_ROOT="${OSDC_UPSTREAM:-$(cd "$MODULE_DIR/../.." && pwd)}"

export NODEPOOLS_DEFS_DIR="$MODULE_DIR/defs"
export NODEPOOLS_OUTPUT_DIR="$MODULE_DIR/generated"
export NODEPOOLS_MODULE_NAME="nodepools-b200"
exec "$UPSTREAM_ROOT/modules/nodepools/deploy.sh" "$@"
