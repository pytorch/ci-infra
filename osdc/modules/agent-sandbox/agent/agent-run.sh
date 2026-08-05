#!/usr/bin/env bash
# Prototype agent entrypoint. Runs ONE task then exits (single-use pod).
#
# Proves the security spine end-to-end:
#   * clones a repo over HTTPS through the agent-vault proxy — the read-only
#     GitHub token is injected on the wire; this process never sees it;
#   * calls Bedrock through the sigv4 proxy — AWS is signed by the proxy's IRSA
#     identity; this process holds no AWS credentials;
#   * writes a plain-text result to /output (consumed by the trusted side).
#
# All egress is forced through the two proxies; the pod has no other route out.
set -euo pipefail

: "${REPO:?set REPO, e.g. pytorch/pytorch}"
: "${REPO_REF:=main}"
: "${TASK:?set TASK — the instruction for the agent}"
: "${BEDROCK_MODEL_ID:?set BEDROCK_MODEL_ID}"
: "${AWS_REGION:?set AWS_REGION}"
: "${SIGV4_PROXY:?set SIGV4_PROXY host:port}"

OUT=/output
mkdir -p "${OUT}"
LOG="${OUT}/agent.log"
REPORT="${OUT}/report.md"

log() { echo "[agent] $*" | tee -a "${LOG}"; }

log "clone https://github.com/${REPO}@${REPO_REF} via agent-vault (${HTTPS_PROXY:-<none>})"
# HTTPS_PROXY + the trusted CA are set by the pod env. The proxy injects the
# read-only token; a bare unauthenticated clone URL is all we send.
git clone --depth 1 --branch "${REPO_REF}" \
  "https://github.com/${REPO}.git" /tmp/repo 2>>"${LOG}"

log "collect a small repo snapshot for context"
TREE="$(cd /tmp/repo && git ls-files | head -n 200)"

# Anthropic-on-Bedrock InvokeModel request. Kept tiny for the prototype.
PROMPT="Task: ${TASK}

Repository ${REPO}@${REPO_REF} top-level files:
${TREE}

Respond with a short markdown analysis."

BODY="$(jq -n --arg p "${PROMPT}" '{
  anthropic_version: "bedrock-2023-05-31",
  max_tokens: 1024,
  messages: [ { role: "user", content: [ { type: "text", text: $p } ] } ]
}')"

log "invoke Bedrock model ${BEDROCK_MODEL_ID} via sigv4 proxy (${SIGV4_PROXY})"
# Unsigned POST to the proxy; Host header names the real AWS endpoint so the
# proxy signs for the bedrock service with its IRSA credentials.
HTTP_CODE="$(curl -sS -o "${OUT}/bedrock-response.json" -w '%{http_code}' \
  -X POST "http://${SIGV4_PROXY}/model/${BEDROCK_MODEL_ID}/invoke" \
  -H "Host: bedrock-runtime.${AWS_REGION}.amazonaws.com" \
  -H "Content-Type: application/json" \
  -d "${BODY}" 2>>"${LOG}")"

if [[ "${HTTP_CODE}" != "200" ]]; then
  log "ERROR: Bedrock returned HTTP ${HTTP_CODE}"
  cat "${OUT}/bedrock-response.json" >>"${LOG}" 2>/dev/null || true
  exit 1
fi

# Anthropic response shape: .content[0].text
jq -r '.content[0].text // "no text in response"' "${OUT}/bedrock-response.json" >"${REPORT}"

log "done — wrote ${REPORT} ($(wc -c <"${REPORT}") bytes)"
