#!/usr/bin/env bash
# Validates a Renovate-style runner_image_tag bump patch.
#
# Defense-in-depth gate shared by osdc-renovate-autoapprove.yml (pre-merge,
# operates on PR file list) and osdc-auto-update-deploy-prod.yml (post-merge,
# operates on the merge commit). Keeping the bit-for-bit identical regex,
# line-count, and monotonicity rules in one place avoids drift between the
# two enforcement points.
#
# Inputs (env vars):
#   PATCH_JSON  — JSON from `gh api repos/X/pulls/N/files` (top-level array)
#                 OR `gh api repos/X/commits/SHA` (object with .files).
#                 Shape is auto-detected: array => PR form; object => commit form.
#
# Outputs (stdout, KEY=VALUE lines):
#   decision=approve | close-wrong-file-count | close-wrong-file |
#            close-no-patch | close-multi-line | close-bad-pattern |
#            close-no-change | close-downgrade
#   reason=<human-readable explanation>
#   old_ver=<X.Y.Z>  (only when decision=approve)
#   new_ver=<X.Y.Z>  (only when decision=approve)
#
# Exit code: 0 on every deterministic decision (callers branch on `decision`).
# Non-zero only for unrecoverable errors (missing jq, malformed JSON, missing
# PATCH_JSON env var).

set -euo pipefail

if [ -z "${PATCH_JSON:-}" ]; then
  echo "validate-runner-bump.sh: PATCH_JSON env var is required" >&2
  exit 2
fi

if ! command -v jq >/dev/null 2>&1; then
  echo "validate-runner-bump.sh: jq is required" >&2
  exit 2
fi

# Auto-detect shape: top-level array (PR form) vs object with .files (commit form).
FILES_JSON=$(printf '%s' "$PATCH_JSON" | jq -c 'if type == "array" then . else (.files // []) end')

NUM_FILES=$(printf '%s' "$FILES_JSON" | jq 'length')
if [ "$NUM_FILES" != "1" ]; then
  printf 'decision=close-wrong-file-count\n'
  printf 'reason=expected exactly 1 file changed; got %s\n' "$NUM_FILES"
  exit 0
fi

FILE_PATH=$(printf '%s' "$FILES_JSON" | jq -r '.[0].filename')
if [ "$FILE_PATH" != "osdc/clusters.yaml" ]; then
  printf 'decision=close-wrong-file\n'
  printf 'reason=expected only osdc/clusters.yaml to change; got %s\n' "$FILE_PATH"
  exit 0
fi

PATCH=$(printf '%s' "$FILES_JSON" | jq -r '.[0].patch')
if [ "$PATCH" = "null" ] || [ -z "$PATCH" ]; then
  printf 'decision=close-no-patch\n'
  printf 'reason=GitHub returned no patch for this file (likely binary, rename-only, or oversized)\n'
  exit 0
fi

ADDED=$(echo "$PATCH" | grep -E '^\+[^+]' || true)
REMOVED=$(echo "$PATCH" | grep -E '^-[^-]' || true)
ADDED_COUNT=$(printf '%s' "$ADDED" | grep -c '^' || true)
REMOVED_COUNT=$(printf '%s' "$REMOVED" | grep -c '^' || true)
if [ "$ADDED_COUNT" != "1" ] || [ "$REMOVED_COUNT" != "1" ]; then
  printf 'decision=close-multi-line\n'
  printf 'reason=expected exactly 1 line added + 1 line removed; got +%s/-%s\n' "$ADDED_COUNT" "$REMOVED_COUNT"
  exit 0
fi

# Strict pattern: leading whitespace + runner_image_tag: "X.Y.Z" + optional trailing comment.
# MUST remain bit-for-bit identical between both workflows to guarantee that the
# pre-merge and post-merge gates accept/reject the same set of patches.
PATTERN='^    runner_image_tag:[[:space:]]*"[0-9]+\.[0-9]+\.[0-9]+"[[:space:]]*(#.*)?$'
NEW_LINE="${ADDED#+}"
OLD_LINE="${REMOVED#-}"
if ! echo "$NEW_LINE" | grep -qE "$PATTERN"; then
  printf 'decision=close-bad-pattern\n'
  printf 'reason=new line does not match runner_image_tag semver pattern: %s\n' "$NEW_LINE"
  exit 0
fi
if ! echo "$OLD_LINE" | grep -qE "$PATTERN"; then
  printf 'decision=close-bad-pattern\n'
  printf 'reason=old line does not match runner_image_tag semver pattern: %s\n' "$OLD_LINE"
  exit 0
fi

OLD_VER=$(printf '%s' "$OLD_LINE" | sed -nE 's/^    runner_image_tag:[[:space:]]*"([0-9]+\.[0-9]+\.[0-9]+)".*$/\1/p')
NEW_VER=$(printf '%s' "$NEW_LINE" | sed -nE 's/^    runner_image_tag:[[:space:]]*"([0-9]+\.[0-9]+\.[0-9]+)".*$/\1/p')
if [ -z "$OLD_VER" ] || [ -z "$NEW_VER" ]; then
  printf 'decision=close-bad-pattern\n'
  printf "reason=failed to extract version strings (old='%s' new='%s')\n" "$OLD_VER" "$NEW_VER"
  exit 0
fi
if [ "$OLD_VER" = "$NEW_VER" ]; then
  printf 'decision=close-no-change\n'
  printf 'reason=no version change (%s -> %s)\n' "$OLD_VER" "$NEW_VER"
  exit 0
fi

# Monotonicity: reject downgrades (defense against a compromised RENOVATE_TOKEN
# rolling back to a known-vulnerable runner version).
HIGHEST=$(printf '%s\n%s\n' "$NEW_VER" "$OLD_VER" | sort -V | tail -1)
if [ "$HIGHEST" != "$NEW_VER" ]; then
  printf 'decision=close-downgrade\n'
  printf 'reason=downgrade detected (%s -> %s)\n' "$OLD_VER" "$NEW_VER"
  exit 0
fi

printf 'decision=approve\n'
printf 'reason=%s -> %s\n' "$OLD_VER" "$NEW_VER"
printf 'old_ver=%s\n' "$OLD_VER"
printf 'new_ver=%s\n' "$NEW_VER"
