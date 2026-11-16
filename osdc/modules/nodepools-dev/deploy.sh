#!/usr/bin/env bash
set -euo pipefail
#
# Dev-cluster NodePools — delegates to upstream deploy with a minimal set of
# local definitions (the runner pool plus the workflow fleets the dev runner
# types need). Replaces the full `nodepools` module on cost-minimal clusters;
# never enable both on one cluster (fleet names collide by design).
# Called by: just deploy-module <cluster> nodepools-dev

MODULE_DIR="$(cd "$(dirname "$0")" && pwd)"
UPSTREAM_ROOT="${OSDC_UPSTREAM:-$(cd "$MODULE_DIR/../.." && pwd)}"

export NODEPOOLS_DEFS_DIR="$MODULE_DIR/defs"
export NODEPOOLS_OUTPUT_DIR="$MODULE_DIR/generated"
export NODEPOOLS_MODULE_NAME="nodepools-dev"
exec "$UPSTREAM_ROOT/modules/nodepools/deploy.sh" "$@"
