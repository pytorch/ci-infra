#!/usr/bin/env bash
set -euo pipefail
#
# Dev-cluster ARC runners — delegates to upstream deploy with a small set of
# local runner definitions. Cluster-wide settings (github_config_url,
# runner_name_prefix, listener sizing) still come from the cluster's
# `arc-runners:` block in clusters.yaml. Pairs with the nodepools-dev module.
# Called by: just deploy-module <cluster> arc-runners-dev

MODULE_DIR="$(cd "$(dirname "$0")" && pwd)"
UPSTREAM_ROOT="${OSDC_UPSTREAM:-$(cd "$MODULE_DIR/../.." && pwd)}"

export ARC_RUNNERS_DEFS_DIR="$MODULE_DIR/defs"
export ARC_RUNNERS_OUTPUT_DIR="$MODULE_DIR/generated"
export ARC_RUNNERS_MODULE_NAME="arc-runners-dev"
export NODEPOOLS_DEFS_DIR="$UPSTREAM_ROOT/modules/nodepools-dev/defs"
exec "$UPSTREAM_ROOT/modules/arc-runners/deploy.sh" "$@"
