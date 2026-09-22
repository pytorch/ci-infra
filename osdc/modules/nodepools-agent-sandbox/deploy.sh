#!/usr/bin/env bash
set -euo pipefail
#
# nodepools-agent-sandbox: thin shim that delegates to the base nodepools module
# with this module's own defs/ + generated/ + module label. Mirrors the
# nodepools-h100 / nodepools-b200 pattern so the AI-agent sandbox fleet
# (gVisor-enabled) is generated and applied only on clusters that list this
# module — it never lands on prod via the shared modules/nodepools defs.
#
# Called by: just deploy-module <cluster> nodepools-agent-sandbox
# Args: $1=cluster-id  $2=cluster-name  $3=region

MODULE_DIR="$(cd "$(dirname "$0")" && pwd)"
UPSTREAM_ROOT="${OSDC_UPSTREAM:-$(cd "$MODULE_DIR/../.." && pwd)}"

export NODEPOOLS_DEFS_DIR="$MODULE_DIR/defs"
export NODEPOOLS_OUTPUT_DIR="$MODULE_DIR/generated"
export NODEPOOLS_MODULE_NAME="nodepools-agent-sandbox"

exec "$UPSTREAM_ROOT/modules/nodepools/deploy.sh" "$@"
