#!/usr/bin/env bash
# Validate ARC runner configs for Guaranteed QoS.
# Checks that all job pod hook templates have resources.requests == resources.limits
# with integer CPU values.
#
# Operates on: modules/arc-runners/generated/*.yaml
# Called by: modules/arc-runners/deploy.sh before deploying
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODULE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
GENERATED_DIR="${ARC_RUNNERS_OUTPUT_DIR:-$MODULE_DIR/generated}"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

ERRORS=0
WARNINGS=0

validate_runner_config() {
  local file=$1
  local filename
  filename=$(basename "$file")
  echo "→ Validating: $filename"

  # Extract the job pod spec from the ConfigMap (after the --- separator)
  local job_spec
  job_spec=$(awk '/^---$/,0' "$file")

  if [[ -z "$job_spec" ]]; then
    echo -e "  ${RED}✗${NC} No ConfigMap found (missing --- separator)"
    ERRORS=$((ERRORS + 1))
    return
  fi

  # Extract resource values from $job container
  local cpu_limit cpu_request mem_limit mem_request
  cpu_limit=$(echo "$job_spec" | awk '/- name: "\$job"/,/^[[:space:]]*$/' | grep -A 10 "limits:" | grep "cpu:" | head -1 | awk '{print $2}' | tr -d '"' | xargs 2>/dev/null || echo "")
  cpu_request=$(echo "$job_spec" | awk '/- name: "\$job"/,/^[[:space:]]*$/' | grep -A 10 "requests:" | grep "cpu:" | head -1 | awk '{print $2}' | tr -d '"' | xargs 2>/dev/null || echo "")
  mem_limit=$(echo "$job_spec" | awk '/- name: "\$job"/,/^[[:space:]]*$/' | grep -A 10 "limits:" | grep "memory:" | head -1 | awk '{print $2}' | tr -d '"' | xargs 2>/dev/null || echo "")
  mem_request=$(echo "$job_spec" | awk '/- name: "\$job"/,/^[[:space:]]*$/' | grep -A 10 "requests:" | grep "memory:" | head -1 | awk '{print $2}' | tr -d '"' | xargs 2>/dev/null || echo "")

  # GPU resources (optional)
  local gpu_limit gpu_request
  gpu_limit=$(echo "$job_spec" | awk '/- name: "\$job"/,/^[[:space:]]*$/' | grep -A 10 "limits:" | grep "nvidia.com/gpu:" | head -1 | awk '{print $2}' | tr -d '"' | xargs 2>/dev/null || echo "")
  gpu_request=$(echo "$job_spec" | awk '/- name: "\$job"/,/^[[:space:]]*$/' | grep -A 10 "requests:" | grep "nvidia.com/gpu:" | head -1 | awk '{print $2}' | tr -d '"' | xargs 2>/dev/null || echo "")

  # Validate CPU: must exist, must match, must be integer
  if [[ -z "$cpu_limit" ]] || [[ -z "$cpu_request" ]]; then
    echo -e "  ${RED}✗${NC} Missing CPU limits or requests"
    ERRORS=$((ERRORS + 1))
  elif [[ "$cpu_limit" != "$cpu_request" ]]; then
    echo -e "  ${RED}✗${NC} CPU mismatch: limits=$cpu_limit, requests=$cpu_request (must be equal for Guaranteed QoS)"
    ERRORS=$((ERRORS + 1))
  elif ! echo "$cpu_limit" | grep -qE '^[0-9]+$'; then
    echo -e "  ${RED}✗${NC} CPU must be integer: $cpu_limit (e.g., \"4\" not \"4000m\")"
    ERRORS=$((ERRORS + 1))
  else
    echo -e "  ${GREEN}✓${NC} CPU: $cpu_limit (Guaranteed QoS)"
  fi

  # Validate Memory: must exist, must match
  if [[ -z "$mem_limit" ]] || [[ -z "$mem_request" ]]; then
    echo -e "  ${RED}✗${NC} Missing memory limits or requests"
    ERRORS=$((ERRORS + 1))
  elif [[ "$mem_limit" != "$mem_request" ]]; then
    echo -e "  ${RED}✗${NC} Memory mismatch: limits=$mem_limit, requests=$mem_request (must be equal for Guaranteed QoS)"
    ERRORS=$((ERRORS + 1))
  else
    echo -e "  ${GREEN}✓${NC} Memory: $mem_limit (Guaranteed QoS)"
  fi

  # Validate GPU if present: must match
  if [[ -n "$gpu_limit" ]] || [[ -n "$gpu_request" ]]; then
    if [[ "$gpu_limit" != "$gpu_request" ]]; then
      echo -e "  ${RED}✗${NC} GPU mismatch: limits=$gpu_limit, requests=$gpu_request"
      ERRORS=$((ERRORS + 1))
    else
      echo -e "  ${GREEN}✓${NC} GPU: $gpu_limit"
    fi
  fi

  # Warn on odd CPU counts (topology manager prefers even)
  if [[ -n "$cpu_limit" ]] && [[ "$cpu_limit" =~ ^[0-9]+$ ]]; then
    if ((cpu_limit % 2 != 0)); then
      echo -e "  ${YELLOW}⚠${NC}  Odd CPU count ($cpu_limit). Topology manager works best with even counts."
      WARNINGS=$((WARNINGS + 1))
    fi
  fi

  echo ""
}

main() {
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo "Runner QoS Validation"
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo ""

  if [[ ! -d "$GENERATED_DIR" ]] || [[ -z "$(ls -A "$GENERATED_DIR"/*.yaml 2>/dev/null)" ]]; then
    echo -e "${RED}No generated runner configs found in $GENERATED_DIR${NC}"
    echo "Run generate_runners.py first."
    exit 1
  fi

  local count=0
  for config in "$GENERATED_DIR"/*.yaml; do
    validate_runner_config "$config"
    count=$((count + 1))
  done

  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo "Configs checked: $count | Errors: $ERRORS | Warnings: $WARNINGS"
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

  if [[ $ERRORS -gt 0 ]]; then
    echo -e "${RED}Validation FAILED${NC}"
    exit 1
  fi

  echo -e "${GREEN}All runners have Guaranteed QoS configuration.${NC}"
  exit 0
}

main "$@"
