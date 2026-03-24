#!/usr/bin/env bash
# Wrapper around `aws eks update-kubeconfig` that serializes concurrent calls
# using a directory-based mutex (mkdir is atomic on local POSIX filesystems).
# Usage: kubeconfig-lock.sh [aws eks update-kubeconfig args...]
set -euo pipefail

LOCKDIR="/tmp/osdc-kubeconfig.lock"
TIMEOUT=90
STALE=45
WAITED=0

# Acquire lock
while ! mkdir "$LOCKDIR" 2>/dev/null; do
  # Stale lock detection: if lock is older than STALE seconds, break it
  if [[ -d "$LOCKDIR" ]]; then
    lock_mtime=$(stat -f %m "$LOCKDIR" 2>/dev/null || stat -c %Y "$LOCKDIR" 2>/dev/null || echo 0)
    lock_age=$(($(date +%s) - lock_mtime))
    if ((lock_age > STALE)); then
      echo "Breaking stale kubeconfig lock (${lock_age}s old)" >&2
      rmdir "$LOCKDIR" 2>/dev/null || true
      continue
    fi
  fi
  if ((WAITED >= TIMEOUT)); then
    echo "ERROR: Timed out waiting for kubeconfig lock after ${TIMEOUT}s" >&2
    exit 1
  fi
  sleep 1
  ((WAITED++))
done

# Release lock on exit (normal, error, or signal)
trap 'rmdir "$LOCKDIR" 2>/dev/null || true' EXIT

# Run the actual command
aws eks update-kubeconfig "$@"
