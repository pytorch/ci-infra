#!/usr/bin/env bash
set -euo pipefail
#
# Launch one sandbox agent run. Renders job-template.yaml.tpl and applies it.
# This is the operator/PoC entry point; a GitHub Action would do the same thing.
#
# Usage:
#   modules/agent-sandbox/run-agent.sh <cluster-id> <repo> <ref> <task> <bedrock-model-id> [timeout-secs]
# Example:
#   modules/agent-sandbox/run-agent.sh meta-staging-aws-ue1 pytorch/pytorch main \
#     "Summarize the top-level build layout" anthropic.claude-3-5-sonnet-20240620-v1:0

CLUSTER="${1:?cluster-id}"
REPO="${2:?repo, e.g. pytorch/pytorch}"
REF="${3:?git ref}"
TASK="${4:?task text}"
MODEL_ID="${5:?bedrock model id}"
TIMEOUT="${6:-600}"

MODULE_DIR="$(cd "$(dirname "$0")" && pwd)"
UPSTREAM_ROOT="${OSDC_UPSTREAM:-$(cd "$MODULE_DIR/../.." && pwd)}"
# shellcheck source=/dev/null
source "$UPSTREAM_ROOT/scripts/mise-activate.sh"
CFG="$UPSTREAM_ROOT/scripts/cluster-config.py"

REGION=$(uv run "$CFG" "$CLUSTER" region)
AGENT_IMAGE=$(uv run "$CFG" "$CLUSTER" agent_sandbox.agent_image "")
if [[ -z "$AGENT_IMAGE" ]]; then
  echo "ERROR: agent_sandbox.agent_image not set for $CLUSTER in clusters.yaml" >&2
  exit 1
fi

RUN_ID="$(date +%s)"

echo "[run-agent] cluster=$CLUSTER repo=$REPO@$REF model=$MODEL_ID run=$RUN_ID"
sed \
  -e "s|__RUN_ID__|${RUN_ID}|g" \
  -e "s|__AGENT_IMAGE__|${AGENT_IMAGE}|g" \
  -e "s|__REPO__|${REPO}|g" \
  -e "s|__REPO_REF__|${REF}|g" \
  -e "s|__TASK__|${TASK}|g" \
  -e "s|__BEDROCK_MODEL_ID__|${MODEL_ID}|g" \
  -e "s|__AWS_REGION__|${REGION}|g" \
  -e "s|__TIMEOUT__|${TIMEOUT}|g" \
  "$MODULE_DIR/job-template.yaml.tpl" \
  | kubectl apply -f -

echo "[run-agent] created Job sandbox-agent-${RUN_ID} in ai-sandbox."
echo "[run-agent] logs:   kubectl logs -n ai-sandbox -l app=sandbox-agent -f"
echo "[run-agent] output: kubectl exec ... or read /output via a debug sidecar (prototype)."
