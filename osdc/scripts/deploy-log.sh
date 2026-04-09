#!/usr/bin/env bash
# shellcheck disable=SC2310
#
# Deploy audit logging — records deploy start/finish events as ConfigMaps
# in the osdc-system namespace.
#
# Two tracking levels:
#   Module:  deploy_log_start module <cluster> <module-name>
#   Command: deploy_log_start cmd    <cluster> <command-name>
#
# Usage:
#   source "$OSDC_UPSTREAM/scripts/deploy-log.sh"
#
#   # Record start (returns epoch for duration calculation)
#   START_EPOCH=$(deploy_log_start module "$CLUSTER" "arc")
#
#   # Set up failure trap
#   trap 'deploy_log_finish module "$CLUSTER" "arc" "$START_EPOCH" failed' ERR
#
#   # ... do the deploy ...
#
#   # Record success
#   trap - ERR
#   deploy_log_finish module "$CLUSTER" "arc" "$START_EPOCH"
#
# All functions are NON-FATAL — deploy logging never aborts a deploy.
# Failures emit a warning to stderr and return 0.

readonly _DEPLOY_LOG_NAMESPACE="osdc-system"
readonly _DEPLOY_LOG_LABEL="app.kubernetes.io/managed-by=osdc-deploy-log"
readonly _DEPLOY_LOG_HISTORY_MAX=50

# --- Public API ---

# Record the start of a deploy. Prints epoch timestamp to stdout.
# Args: <scope> <cluster> <name>
#   scope:   "module" or "cmd"
#   cluster: cluster ID (e.g., arc-staging)
#   name:    module name or command name
deploy_log_start() {
  local scope="$1" cluster="$2" name="$3"
  local start_epoch
  start_epoch=$(date +%s)

  (
    _deploy_log_gather_metadata

    local cm_name="osdc-deploy-${scope}-start-${name}"
    local scope_key
    scope_key=$(_deploy_log_scope_key "$scope")

    _deploy_log_write_configmap "$cm_name" \
      "commit=${_DL_COMMIT}" \
      "branch=${_DL_BRANCH}" \
      "user=${_DL_USER}" \
      "cluster=${cluster}" \
      "timestamp=${_DL_TIMESTAMP}" \
      "${scope_key}=${name}" \
      "status=started"

    local json
    json=$(_deploy_log_json_entry start "$cluster" "$scope_key" "$name" "" "started")
    _deploy_log_append_history "osdc-deploy-${scope}-history-${name}" "$json"
  ) || true

  echo "$start_epoch"
}

# Record the end of a deploy.
# Args: <scope> <cluster> <name> <start_epoch> [status]
#   status defaults to "completed"
deploy_log_finish() {
  local scope="$1" cluster="$2" name="$3" start_epoch="$4"
  local status="${5:-completed}"

  (
    _deploy_log_gather_metadata

    local now_epoch
    now_epoch=$(date +%s)
    local duration=$((now_epoch - start_epoch))

    local cm_name="osdc-deploy-${scope}-finish-${name}"
    local scope_key
    scope_key=$(_deploy_log_scope_key "$scope")

    _deploy_log_write_configmap "$cm_name" \
      "commit=${_DL_COMMIT}" \
      "branch=${_DL_BRANCH}" \
      "user=${_DL_USER}" \
      "cluster=${cluster}" \
      "timestamp=${_DL_TIMESTAMP}" \
      "${scope_key}=${name}" \
      "duration=${duration}" \
      "status=${status}"

    local json
    json=$(_deploy_log_json_entry finish "$cluster" "$scope_key" "$name" "$duration" "$status")
    _deploy_log_append_history "osdc-deploy-${scope}-history-${name}" "$json"
  ) || true
}

# --- Internal helpers ---

# Resolve scope to key name: module→"module", cmd→"command"
_deploy_log_scope_key() {
  if [[ "$1" == "module" ]]; then echo "module"; else echo "command"; fi
}

# Gather git and user metadata into _DL_* variables.
_deploy_log_gather_metadata() {
  _DL_COMMIT=$(git rev-parse --short HEAD 2>/dev/null || echo "unknown")
  _DL_BRANCH=$(git branch --show-current 2>/dev/null || echo "")
  [[ -z "$_DL_BRANCH" ]] && _DL_BRANCH="detached"
  _DL_USER="${USER:-unknown}"
  _DL_TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
}

# Write (create-or-update) a ConfigMap with the given key=value pairs.
# Args: <configmap-name> <key=value>...
_deploy_log_write_configmap() {
  local cm_name="$1"
  shift

  local from_literal_args=()
  for kv in "$@"; do
    from_literal_args+=("--from-literal=${kv}")
  done

  if ! kubectl create configmap "$cm_name" \
    -n "$_DEPLOY_LOG_NAMESPACE" \
    -l "$_DEPLOY_LOG_LABEL" \
    "${from_literal_args[@]}" \
    --dry-run=client -o yaml \
    | kubectl apply -f - >/dev/null 2>&1; then
    echo "  Warning: deploy-log failed to write ConfigMap '$cm_name'" >&2
  fi
}

# Escape a string for safe embedding in a JSON value.
# Handles backslashes and double quotes.
_deploy_log_escape_json() {
  local s="$1"
  s="${s//\\/\\\\}" # backslash first
  s="${s//\"/\\\"}" # double quotes
  printf '%s' "$s"
}

# Build a JSONL entry for the history ConfigMap.
# Args: <event> <cluster> <scope_key> <scope_val> <duration> <status>
_deploy_log_json_entry() {
  local event="$1" cluster="$2" scope_key="$3" scope_val="$4"
  local duration="$5" status="$6"

  local json
  json=$(printf '{"ts":"%s","event":"%s","commit":"%s","branch":"%s","user":"%s","cluster":"%s","%s":"%s"' \
    "$(_deploy_log_escape_json "$_DL_TIMESTAMP")" \
    "$(_deploy_log_escape_json "$event")" \
    "$(_deploy_log_escape_json "$_DL_COMMIT")" \
    "$(_deploy_log_escape_json "$_DL_BRANCH")" \
    "$(_deploy_log_escape_json "$_DL_USER")" \
    "$(_deploy_log_escape_json "$cluster")" \
    "$(_deploy_log_escape_json "$scope_key")" \
    "$(_deploy_log_escape_json "$scope_val")")

  if [[ -n "$duration" ]]; then
    json+=",\"duration\":\"${duration}\""
  fi
  if [[ -n "$status" ]]; then
    json+=",\"status\":\"${status}\""
  fi
  json+="}"
  echo "$json"
}

# Append a JSONL line to a history ConfigMap, trimming to _DEPLOY_LOG_HISTORY_MAX.
# Args: <configmap-name> <json-line>
_deploy_log_append_history() {
  local cm_name="$1" new_line="$2"

  # Read existing history (empty string if ConfigMap doesn't exist yet)
  local existing
  existing=$(kubectl get configmap "$cm_name" \
    -n "$_DEPLOY_LOG_NAMESPACE" \
    -o jsonpath='{.data.entries}' 2>/dev/null || echo "")

  # Append new line and trim to max entries
  local updated
  if [[ -n "$existing" ]]; then
    updated=$(printf '%s\n%s' "$existing" "$new_line" | tail -n "$_DEPLOY_LOG_HISTORY_MAX")
  else
    updated="$new_line"
  fi

  # Write back
  if ! kubectl create configmap "$cm_name" \
    -n "$_DEPLOY_LOG_NAMESPACE" \
    -l "$_DEPLOY_LOG_LABEL" \
    --from-literal="entries=${updated}" \
    --dry-run=client -o yaml \
    | kubectl apply -f - >/dev/null 2>&1; then
    echo "  Warning: deploy-log failed to update history '$cm_name'" >&2
  fi
}
